"""The main agent loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from PIL import Image

from .actions import Action, ActionExecutor, ActionParser, StopAction
from .grounding import CoordinateGrounding, GroundingModel


@dataclass
class LoopState:
    """Mutable loop state."""

    objective: str = ""
    paused: bool = False
    done: bool = False
    step_count: int = 0
    last_screenshot_hash: str | None = None
    repeated_screenshot_count: int = 0
    observations: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """Coordinate model reasoning, grounding, and action execution."""

    def __init__(
        self,
        provider: Any,
        action_executor: ActionExecutor,
        desktop_api: Any,
        *,
        grounding: GroundingModel | None = None,
        stuck_threshold: int = 3,
        max_steps: int = 40,
    ) -> None:
        self.provider = provider
        self.action_executor = action_executor
        self.desktop_api = desktop_api
        self.grounding = grounding or CoordinateGrounding()
        self.stuck_threshold = stuck_threshold
        self.max_steps = max_steps
        self.state = LoopState()
        self._resume_event = asyncio.Event()
        self._resume_event.set()

    async def run(self, objective: str) -> list[Action]:
        """Run the loop until the task completes or the step cap is reached."""

        self.state = LoopState(objective=objective)
        executed: list[Action] = []
        for _ in range(self.max_steps):
            if self.state.done:
                break
            await self._resume_event.wait()
            result = await self.step()
            executed.append(result)
            if isinstance(result, StopAction):
                self.state.done = True
                break
        return executed

    async def step(self) -> Action:
        """Execute one model-decision step."""

        if self.state.paused:
            await self._resume_event.wait()
        screenshot = await self.desktop_api.screenshot()
        screenshot_hash = sha256(screenshot).hexdigest()
        if self.state.last_screenshot_hash == screenshot_hash:
            self.state.repeated_screenshot_count += 1
        else:
            self.state.repeated_screenshot_count = 0
        self.state.last_screenshot_hash = screenshot_hash

        if self.state.repeated_screenshot_count >= self.stuck_threshold:
            action = StopAction(reason="stuck-detection: repeated screenshot")
            await self.action_executor.execute(action)
            self.state.done = True
            return action

        messages = self._build_messages(screenshot)
        if await self.provider.is_complete(messages):
            action = StopAction(reason="provider marked task complete")
            await self.action_executor.execute(action)
            self.state.done = True
            return action

        action = await self.provider.get_action(messages)

        # Execute action with one retry on failure
        try:
            await self.action_executor.execute(action)
        except Exception as exc:
            # Retry once with error context injected into history
            self.state.history = getattr(self.state, "history", [])
            self.state.history.append(
                {
                    "role": "system",
                    "content": f"Previous action failed: {exc}. Try a different approach.",
                }
            )
            try:
                messages_with_error = self._build_messages(screenshot)
                action = await self.provider.get_action(messages_with_error)
                await self.action_executor.execute(action)
            except Exception:
                pass  # best effort recovery

        self.state.step_count += 1

        # Judge completion after every step
        try:
            if await self.provider.is_complete(
                self._build_messages(await self.desktop_api.screenshot())
            ):
                action = StopAction(reason="task complete after step")
                self.state.done = True
        except Exception:
            pass  # completion check is best-effort

        return action

    def pause(self) -> None:
        """Pause the loop."""

        self.state.paused = True
        self._resume_event.clear()

    def resume(self) -> None:
        """Resume the loop."""

        self.state.paused = False
        self._resume_event.set()

    def inject_instruction(self, instruction: str) -> None:
        """Inject a human instruction that the provider can observe."""

        self.state.instructions.append(instruction)

    def _build_messages(self, screenshot: bytes) -> list[dict[str, Any]]:
        instruction_text = "\n".join(self.state.instructions[-5:])
        observations = "\n".join(self.state.observations[-5:])
        return [
            {"role": "system", "content": "You are a computer-use agent. Respond with a single JSON action."},
            *getattr(self.state, "history", []),
            {"role": "user", "content": f"Objective: {self.state.objective}\nInstructions: {instruction_text}\nObservations: {observations}"},
            {"role": "user", "content": {"type": "image", "bytes": len(screenshot)}},
        ]

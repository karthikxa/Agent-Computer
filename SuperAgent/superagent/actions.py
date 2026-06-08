"""Action models and execution helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Action(BaseModel):
    """Base action model."""

    model_config = ConfigDict(extra="allow")
    kind: str


class ClickAction(Action):
    """Mouse click action."""

    kind: Literal["click"] = "click"
    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"
    clicks: int = 1


class DragAction(Action):
    """Mouse drag action."""

    kind: Literal["drag"] = "drag"
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    duration_ms: int = 250


class KeyAction(Action):
    """Keyboard action."""

    kind: Literal["key"] = "key"
    keys: list[str]


class ScrollAction(Action):
    """Scroll action."""

    kind: Literal["scroll"] = "scroll"
    dx: int = 0
    dy: int = 0


class TextAction(Action):
    """Type text action."""

    kind: Literal["type"] = "type"
    text: str
    enter: bool = False


class ShellAction(Action):
    """Run a shell command."""

    kind: Literal["shell"] = "shell"
    command: str
    timeout_seconds: float = 30.0


class WaitAction(Action):
    """Pause execution for a period of time."""

    kind: Literal["wait"] = "wait"
    seconds: float = 1.0


class StopAction(Action):
    """Signal task completion."""

    kind: Literal["stop"] = "stop"
    reason: str | None = None


ACTION_TYPES: dict[str, type[Action]] = {
    "click": ClickAction,
    "drag": DragAction,
    "key": KeyAction,
    "scroll": ScrollAction,
    "type": TextAction,
    "shell": ShellAction,
    "wait": WaitAction,
    "stop": StopAction,
}


class ActionParser:
    """Parse model output into an action model."""

    @staticmethod
    def parse(payload: Any) -> Action:
        """Parse an action from JSON, dictionaries, or already-instantiated models."""

        if isinstance(payload, Action):
            return payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="ignore")
        if isinstance(payload, str):
            payload = payload.strip()
            if not payload:
                return StopAction(reason="empty model output")
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return TextAction(text=payload, enter=False)
        if not isinstance(payload, dict):
            return StopAction(reason="unsupported payload type")

        kind = str(payload.get("kind") or payload.get("action") or "").lower()
        action_type = ACTION_TYPES.get(kind)
        if action_type is None:
            text = payload.get("text") or payload.get("message") or json.dumps(payload)
            return TextAction(text=str(text), enter=False)

        normalized = dict(payload)
        normalized["kind"] = kind
        if kind == "key" and "keys" not in normalized:
            key = normalized.pop("key", normalized.pop("name", None))
            normalized["keys"] = [str(key)] if key is not None else []
        return action_type.model_validate(normalized)


@dataclass(slots=True)
class ActionResult:
    """Result returned by the action executor."""

    action: Action
    ok: bool
    output: str = ""
    metadata: dict[str, Any] | None = None


class ActionExecutor:
    """Execute parsed actions against a desktop API client."""

    def __init__(self, desktop_api: Any) -> None:
        self.desktop_api = desktop_api

    async def execute(self, action: Action) -> ActionResult:
        """Execute one action and return a structured result."""

        if isinstance(action, ClickAction):
            await self.desktop_api.click(action.x, action.y, button=action.button, clicks=action.clicks)
            return ActionResult(action=action, ok=True, output="clicked")
        if isinstance(action, DragAction):
            await self.desktop_api.drag(
                action.start_x,
                action.start_y,
                action.end_x,
                action.end_y,
                duration_ms=action.duration_ms,
            )
            return ActionResult(action=action, ok=True, output="dragged")
        if isinstance(action, KeyAction):
            await self.desktop_api.press_keys(action.keys)
            return ActionResult(action=action, ok=True, output="keys sent")
        if isinstance(action, ScrollAction):
            await self.desktop_api.scroll(action.dx, action.dy)
            return ActionResult(action=action, ok=True, output="scrolled")
        if isinstance(action, TextAction):
            await self.desktop_api.type_text(action.text, enter=action.enter)
            return ActionResult(action=action, ok=True, output="typed")
        if isinstance(action, ShellAction):
            result = await self.desktop_api.run_command(action.command, timeout_seconds=action.timeout_seconds)
            return ActionResult(action=action, ok=True, output=result)
        if isinstance(action, WaitAction):
            await self.desktop_api.wait(action.seconds)
            return ActionResult(action=action, ok=True, output="waited")
        if isinstance(action, StopAction):
            return ActionResult(action=action, ok=True, output=action.reason or "stopped")
        return ActionResult(action=action, ok=False, output="unsupported action")

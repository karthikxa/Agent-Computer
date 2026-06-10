"""Three-model pipeline: Grounding → Vision → Action.

Feature #52 — inspired by e2b/open-computer-use.

Pipeline stages
---------------
1. **Grounding model** (OS-Atlas / ShowUI):
   Takes a screenshot and a natural-language query,
   returns the (x, y) pixel coordinates of the target UI element.

2. **Vision model** (GPT-4o / Claude):
   Takes the raw screenshot and the user objective,
   returns a high-level understanding / plan.

3. **Action model** (any BaseProvider):
   Takes vision model output + grounding coordinates,
   returns the next concrete Action to execute.

Usage::

    pipeline = AgentPipeline(
        grounding=OSAtlasGrounding(),
        vision=OpenAIProvider("gpt-4o"),
        action=AnthropicProvider("claude-3.5-sonnet"),
    )
    action = await pipeline.step(screenshot_bytes, objective, history)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .actions import Action, ActionParser
from .grounding import GroundingModel, GroundingResult, CoordinateGrounding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline config
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """Configuration for the three-model pipeline."""

    max_grounding_attempts: int = 3
    vision_temperature: float = 0.2
    action_temperature: float = 0.1
    include_grounding_in_action_prompt: bool = True
    enable_som_tagging: bool = True   # SOM overlay before vision model


# ---------------------------------------------------------------------------
# Pipeline step result
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Full result from one pipeline step."""

    action: Action
    grounding: GroundingResult | None
    vision_summary: str
    latency_ms: float
    stage_latencies: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

class AgentPipeline:
    """Orchestrates grounding → vision → action three-model pipeline."""

    def __init__(
        self,
        grounding: GroundingModel | None = None,
        vision: Any = None,    # BaseProvider
        action: Any = None,    # BaseProvider
        config: PipelineConfig | None = None,
    ) -> None:
        self.grounding = grounding or CoordinateGrounding()
        self.vision = vision
        self.action = action
        self.config = config or PipelineConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def step(
        self,
        screenshot: bytes,
        objective: str,
        history: list[dict[str, Any]] | None = None,
    ) -> PipelineResult:
        """Run one full pipeline step and return the next action.

        Parameters
        ----------
        screenshot:
            Raw PNG bytes of the current desktop state.
        objective:
            The current task/goal the agent is working toward.
        history:
            Prior messages / actions for context.
        """
        t0 = time.monotonic()
        history = history or []
        stage_latencies: dict[str, float] = {}

        # ------- Stage 1: SOM tagging (optional) ------
        tagged_screenshot = screenshot
        if self.config.enable_som_tagging:
            t_som = time.monotonic()
            tagged_screenshot = await self._apply_som(screenshot)
            stage_latencies["som_ms"] = (time.monotonic() - t_som) * 1000

        # ------- Stage 2: Vision model ----------------
        t_vision = time.monotonic()
        vision_summary = await self._vision_stage(tagged_screenshot, objective, history)
        stage_latencies["vision_ms"] = (time.monotonic() - t_vision) * 1000

        # ------- Stage 3: Grounding -------------------
        t_ground = time.monotonic()
        grounding_result = await self._grounding_stage(screenshot, vision_summary)
        stage_latencies["grounding_ms"] = (time.monotonic() - t_ground) * 1000

        # ------- Stage 4: Action model ----------------
        t_action = time.monotonic()
        action = await self._action_stage(
            screenshot, objective, vision_summary, grounding_result, history
        )
        stage_latencies["action_ms"] = (time.monotonic() - t_action) * 1000

        total_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "Pipeline step done in %.0fms (vision=%.0f, ground=%.0f, action=%.0f)",
            total_ms,
            stage_latencies.get("vision_ms", 0),
            stage_latencies.get("grounding_ms", 0),
            stage_latencies.get("action_ms", 0),
        )

        return PipelineResult(
            action=action,
            grounding=grounding_result,
            vision_summary=vision_summary,
            latency_ms=total_ms,
            stage_latencies=stage_latencies,
        )

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    async def _apply_som(self, screenshot: bytes) -> bytes:
        """Apply Self-of-Mark visual tagging to the screenshot."""
        try:
            from .som import SOMVisualTagger, InteractiveElement
            # Auto-detect interactive elements via basic heuristic grid
            elements = self._detect_elements_heuristic(screenshot)
            tagger = SOMVisualTagger()
            tagged, _ = tagger.tag_screenshot(screenshot, elements)
            return tagged
        except Exception as exc:
            logger.debug("SOM tagging failed, using raw screenshot: %s", exc)
            return screenshot

    def _detect_elements_heuristic(self, screenshot: bytes) -> list[Any]:
        """Rough grid-based element detection when no model available."""
        from .som import InteractiveElement
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(screenshot))
            w, h = img.size
            # 3x3 grid of regions as placeholder interactive elements
            elements = []
            cols, rows = 3, 3
            for i in range(rows):
                for j in range(cols):
                    x1 = j * (w // cols)
                    y1 = i * (h // rows)
                    x2 = x1 + (w // cols)
                    y2 = y1 + (h // rows)
                    eid = f"{i * cols + j + 1}"
                    elements.append(InteractiveElement(eid, x1, y1, x2, y2))
            return elements
        except Exception:
            return []

    async def _vision_stage(
        self, screenshot: bytes, objective: str, history: list[dict[str, Any]]
    ) -> str:
        """Stage 2: Vision model interprets the screen state."""
        if self.vision is None:
            return f"Objective: {objective}. No vision model configured."

        screenshot_b64 = base64.b64encode(screenshot).decode()
        messages = [
            *history[-6:],  # last 6 messages for context
            {
                "role": "user",
                "screenshot_b64": screenshot_b64,
                "text": (
                    f"You are a vision assistant analyzing a desktop screenshot.\n"
                    f"Current objective: {objective}\n\n"
                    f"Describe what you see on screen in 2-3 sentences relevant to "
                    f"completing the objective. Focus on UI elements, text, and state."
                ),
            },
        ]
        try:
            response = await self.vision.chat(
                messages, temperature=self.config.vision_temperature
            )
            return response.content
        except Exception as exc:
            logger.warning("Vision stage failed: %s", exc)
            return f"Objective: {objective}."

    async def _grounding_stage(
        self, screenshot: bytes, vision_summary: str
    ) -> GroundingResult | None:
        """Stage 3: Grounding model pinpoints the relevant UI element."""
        if self.grounding is None:
            return None
        try:
            result = await self.grounding.locate(screenshot, vision_summary)
            logger.debug(
                "Grounding: (%d, %d) confidence=%.2f", result.x, result.y, result.confidence
            )
            return result
        except Exception as exc:
            logger.warning("Grounding stage failed: %s", exc)
            return None

    async def _action_stage(
        self,
        screenshot: bytes,
        objective: str,
        vision_summary: str,
        grounding: GroundingResult | None,
        history: list[dict[str, Any]],
    ) -> Action:
        """Stage 4: Action model decides the next concrete action."""
        if self.action is None:
            # Fallback: generate a wait action
            from .actions import WaitAction
            return WaitAction(seconds=1.0)

        screenshot_b64 = base64.b64encode(screenshot).decode()
        grounding_hint = ""
        if grounding and self.config.include_grounding_in_action_prompt:
            grounding_hint = (
                f"\nGrounding model found target at pixel ({grounding.x}, {grounding.y}) "
                f"with confidence {grounding.confidence:.2f}."
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a computer-use agent. Given a screenshot and objective, "
                    "output the next action as JSON.\n"
                    "Actions: click(x,y), type(text), scroll(x,y,dy), "
                    "run_command(cmd), wait(seconds), stop(reason)."
                ),
            },
            *history[-4:],
            {
                "role": "user",
                "screenshot_b64": screenshot_b64,
                "text": (
                    f"Objective: {objective}\n"
                    f"Screen state: {vision_summary}"
                    f"{grounding_hint}\n\n"
                    f"Output the next action JSON."
                ),
            },
        ]
        try:
            return await self.action.get_action(
                messages, temperature=self.config.action_temperature
            )
        except Exception as exc:
            logger.error("Action stage failed: %s", exc)
            from .actions import WaitAction
            return WaitAction(seconds=2.0)


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def build_pipeline(
    grounding_provider: str = "osatlas",
    vision_provider: str = "openai",
    vision_model: str = "gpt-4o",
    action_provider: str = "anthropic",
    action_model: str = "claude-3.5-sonnet",
    *,
    api_keys: dict[str, str] | None = None,
) -> AgentPipeline:
    """Convenience factory to build a three-model pipeline from provider names."""
    from .providers import create_provider
    from .grounding import OSAtlasGrounding, CoordinateGrounding

    api_keys = api_keys or {}

    grounding: GroundingModel
    if grounding_provider == "osatlas":
        grounding = OSAtlasGrounding()
    else:
        grounding = CoordinateGrounding()

    vision = create_provider(
        vision_provider, vision_model, api_key=api_keys.get(vision_provider)
    )
    action = create_provider(
        action_provider, action_model, api_key=api_keys.get(action_provider)
    )

    return AgentPipeline(grounding=grounding, vision=vision, action=action)

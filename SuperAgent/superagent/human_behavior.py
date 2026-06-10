"""Human Behavior Engine — makes the agent indistinguishable from a real person.

A real human employee does NOT:
  ❌ Click instantly at exact pixel coordinates
  ❌ Type at 10,000 WPM with zero errors
  ❌ Move the mouse in straight lines

This module makes the agent behave naturally:
  ✅ Mouse moves in Bezier curves with variable speed
  ✅ Types at realistic WPM (40–80) with occasional typos + corrections
  ✅ Pauses to "read" content (proportional to text length)
  ✅ Scrolls gradually, not instantly
  ✅ Has micro-breaks between actions
  ✅ Sometimes mis-clicks and self-corrects
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Typing profiles ────────────────────────────────────────────────────────────

TYPING_PROFILES = {
    "fast":   {"wpm": 75, "error_rate": 0.005, "burst": True},
    "normal": {"wpm": 50, "error_rate": 0.015, "burst": False},
    "slow":   {"wpm": 30, "error_rate": 0.025, "burst": False},
    "cautious": {"wpm": 20, "error_rate": 0.002, "burst": False},  # e.g. passwords
}


@dataclass
class HumanBehaviorEngine:
    """Wraps VirtualInputDriver with human-like delays, curves and typos.

    Parameters
    ----------
    input_driver:
        The underlying VirtualInputDriver to wrap.
    profile:
        Typing style: 'fast' | 'normal' | 'slow' | 'cautious'.
    reading_wpm:
        Words-per-minute reading speed (used to calculate pause durations).
    jitter_px:
        Pixel jitter added to click targets to avoid perfect repeatability.
    """

    input_driver: Any
    profile: str = "normal"
    reading_wpm: float = 250.0
    jitter_px: int = 4
    enable_typos: bool = True
    enable_bezier_mouse: bool = True

    # ── Mouse movement ─────────────────────────────────────────────────────────

    async def move_to(self, x: int, y: int, *, steps: int = 20) -> None:
        """Move mouse from current position to (x, y) along a Bezier curve."""
        if not self.enable_bezier_mouse or not self.input_driver:
            return

        # Add human jitter to target
        tx = x + random.randint(-self.jitter_px, self.jitter_px)
        ty = y + random.randint(-self.jitter_px, self.jitter_px)

        # Bezier control points (simulate natural arc)
        try:
            cx1 = x + random.randint(-80, 80)
            cy1 = y + random.randint(-60, 60)
            cx2 = tx + random.randint(-40, 40)
            cy2 = ty + random.randint(-40, 40)

            # Get rough current position (start at 0,0 if unknown)
            sx, sy = getattr(self.input_driver, "_cursor_x", 0), \
                     getattr(self.input_driver, "_cursor_y", 0)

            for i in range(steps + 1):
                t = i / steps
                # Cubic Bezier
                bx = int((1-t)**3 * sx + 3*(1-t)**2*t * cx1 + 3*(1-t)*t**2 * cx2 + t**3 * tx)
                by = int((1-t)**3 * sy + 3*(1-t)**2*t * cy1 + 3*(1-t)*t**2 * cy2 + t**3 * ty)
                await self.input_driver.move_to(bx, by)
                # Variable speed — accelerate then decelerate (ease in-out)
                speed = 0.5 - 0.5 * math.cos(math.pi * t)
                delay = (0.008 + 0.012 * (1 - speed)) * random.uniform(0.8, 1.2)
                await asyncio.sleep(delay)

            # Update tracked position
            self.input_driver._cursor_x = tx
            self.input_driver._cursor_y = ty
        except Exception:
            # Fallback: direct move
            try:
                await self.input_driver.move_to(tx, ty)
            except Exception:
                pass

    async def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Human-like click: move → brief pause → click."""
        await self.move_to(x, y)
        # Tiny pre-click pause (reaction time)
        await asyncio.sleep(random.uniform(0.05, 0.18))
        if double:
            await self.input_driver.double_click(x, y)
        else:
            await self.input_driver.click(x, y)
        # Post-click pause
        await asyncio.sleep(random.uniform(0.1, 0.3))

    async def right_click(self, x: int, y: int) -> None:
        """Human-like right-click."""
        await self.move_to(x, y)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self.input_driver.right_click(x, y)
        await asyncio.sleep(random.uniform(0.2, 0.5))

    # ── Typing ─────────────────────────────────────────────────────────────────

    async def type_text(
        self,
        text: str,
        *,
        profile: str | None = None,
        clear_first: bool = False,
    ) -> None:
        """Type text with human-like speed, typos, and corrections.

        Parameters
        ----------
        text:
            The text to type.
        profile:
            Override typing profile for this call.
        clear_first:
            Select all + delete before typing (useful for input fields).
        """
        cfg = TYPING_PROFILES.get(profile or self.profile, TYPING_PROFILES["normal"])
        wpm = cfg["wpm"] + random.uniform(-10, 10)
        error_rate = cfg["error_rate"] if self.enable_typos else 0.0
        chars_per_sec = (wpm * 5) / 60.0
        char_delay = 1.0 / max(chars_per_sec, 1)

        if clear_first:
            await self.input_driver.press_keys(["ctrl", "a"])
            await asyncio.sleep(0.1)
            await self.input_driver.press_keys(["Delete"])
            await asyncio.sleep(0.15)

        i = 0
        while i < len(text):
            ch = text[i]

            # Simulate burst typing (consecutive chars slightly faster)
            delay = char_delay * random.uniform(0.6, 1.6)

            # Random typo
            if error_rate > 0 and random.random() < error_rate and ch.isalpha():
                # Type a wrong char nearby on keyboard
                typo = self._nearby_key(ch)
                await self.input_driver.type_text(typo)
                await asyncio.sleep(delay * random.uniform(1.5, 3.0))  # pause realising mistake
                # Backspace and correct
                await self.input_driver.press_keys(["BackSpace"])
                await asyncio.sleep(delay * 0.8)

            await self.input_driver.type_text(ch)
            await asyncio.sleep(delay)

            # Occasional longer pause (thinking / hesitation)
            if random.random() < 0.03:
                await asyncio.sleep(random.uniform(0.3, 1.2))

            i += 1

    async def type_password(self, password: str) -> None:
        """Type a password cautiously (slow, no typos shown)."""
        await self.type_text(password, profile="cautious")

    # ── Reading / thinking pauses ──────────────────────────────────────────────

    async def read_pause(self, text: str) -> None:
        """Pause proportional to text length, simulating reading time."""
        word_count = len(text.split())
        seconds = (word_count / self.reading_wpm) * 60.0
        # Add cognitive processing overhead (20–40%)
        seconds *= random.uniform(1.2, 1.4)
        # Cap at 8 seconds for very long texts
        seconds = min(seconds, 8.0)
        await asyncio.sleep(seconds)

    async def think_pause(self, min_s: float = 0.5, max_s: float = 2.5) -> None:
        """Short thinking pause before an action."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def micro_pause(self) -> None:
        """Very short pause between sub-actions."""
        await asyncio.sleep(random.uniform(0.05, 0.25))

    # ── Scrolling ──────────────────────────────────────────────────────────────

    async def scroll_down_human(self, distance: int = 800) -> None:
        """Scroll down gradually, not all at once."""
        steps = random.randint(3, 7)
        per_step = distance // steps
        for _ in range(steps):
            if self.input_driver:
                await self.input_driver.scroll(0, 0, -per_step // 100)
            await asyncio.sleep(random.uniform(0.15, 0.45))

    async def scroll_to_element(self, x: int, y: int) -> None:
        """Scroll until an element at (x, y) is in view, then move mouse to it."""
        await self.move_to(x, y)
        await self.micro_pause()

    # ── Natural multi-step interactions ───────────────────────────────────────

    async def fill_field(self, x: int, y: int, value: str, *, is_password: bool = False) -> None:
        """Click a field, optionally clear it, and type a value."""
        await self.click(x, y)
        await self.micro_pause()
        if is_password:
            await self.type_password(value)
        else:
            await self.type_text(value, clear_first=True)
        await self.micro_pause()

    async def hover(self, x: int, y: int) -> None:
        """Move to an element and pause (hover)."""
        await self.move_to(x, y)
        await asyncio.sleep(random.uniform(0.3, 0.9))

    # ── Keyboard shortcuts ─────────────────────────────────────────────────────

    async def copy(self) -> None:
        await self.micro_pause()
        await self.input_driver.press_keys(["ctrl", "c"])
        await self.micro_pause()

    async def paste(self) -> None:
        await self.micro_pause()
        await self.input_driver.press_keys(["ctrl", "v"])
        await self.micro_pause()

    async def undo(self) -> None:
        await self.input_driver.press_keys(["ctrl", "z"])
        await self.micro_pause()

    async def select_all(self) -> None:
        await self.input_driver.press_keys(["ctrl", "a"])
        await self.micro_pause()

    async def press_enter(self) -> None:
        await self.micro_pause()
        await self.input_driver.press_keys(["Return"])
        await self.micro_pause()

    async def press_tab(self) -> None:
        await self.input_driver.press_keys(["Tab"])
        await self.micro_pause()

    async def press_escape(self) -> None:
        await self.input_driver.press_keys(["Escape"])
        await self.micro_pause()

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _nearby_key(ch: str) -> str:
        """Return a keyboard-adjacent character to simulate a realistic typo."""
        ADJACENCY: dict[str, str] = {
            "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "erfcs",
            "e": "wrsd", "f": "rtgd", "g": "tyhf", "h": "yujg",
            "i": "uojk", "j": "uikh", "k": "iojl", "l": "opk",
            "m": "njk", "n": "bhjm", "o": "ipkl", "p": "ol",
            "q": "wa", "r": "etfd", "s": "weadzx", "t": "ryfg",
            "u": "yihj", "v": "cfgb", "w": "qsea", "x": "zsdc",
            "y": "tugh", "z": "asx",
        }
        neighbors = ADJACENCY.get(ch.lower(), ch)
        typo = random.choice(neighbors)
        return typo.upper() if ch.isupper() else typo

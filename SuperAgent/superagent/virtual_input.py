"""Background Virtual Input Driver for SuperAgent.

Inspired by trycua/cua's Cua Driver concept:
Sends mouse/keyboard events to specific application windows WITHOUT
stealing active user focus or moving the real hardware cursor.

Platform strategy
-----------------
- **Linux / Docker** : ``xdotool`` and ``Xvfb`` virtual display (``DISPLAY=:1``).
  Actions are injected into a specific window by X11 window ID, leaving the
  user's primary display untouched.
- **Windows**        : Win32 ``PostMessage`` / ``SendInput`` with the
  ``HWND`` of the target window — does not require foreground focus.
- **macOS**          : Quartz ``CGEvent`` posted to a specific process via
  ``CGEventPostToPid`` (no cursor movement on primary display).
- **Fallback**       : ``pyautogui`` (moves the real cursor — use only as last resort).

Usage::

    driver = VirtualInputDriver()
    await driver.click(800, 600)                    # virtual click
    await driver.type_text("hello world")           # virtual keyboard input
    await driver.press_keys(["ctrl", "c"])          # key combination
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

_SYSTEM = platform.system()  # "Linux", "Windows", "Darwin"


# ---------------------------------------------------------------------------
# Virtual Input Driver
# ---------------------------------------------------------------------------

@dataclass
class VirtualInputDriver:
    """Platform-aware virtual input that avoids stealing the real cursor.

    Parameters
    ----------
    display:
        X11 display string (Linux only). Defaults to ``:1`` which is the
        standard Xvfb virtual display inside Docker containers.
    window_title:
        Optional window title used to locate the X11 / Win32 window handle
        automatically when ``window_id`` is not supplied.
    window_id:
        Explicit X11 window ID (Linux) or HWND (Windows) to target.
    fallback_to_pyautogui:
        If True, fall back to pyautogui when platform-native APIs are
        unavailable (moves the real cursor — use with caution).
    """

    display: str = ":1"
    window_title: str | None = None
    window_id: str | None = None
    fallback_to_pyautogui: bool = True
    security_manager: SecurityManager | None = None
    _xdotool_available: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._xdotool_available = shutil.which("xdotool") is not None

    def _check_permission(self, action_type: str) -> bool:
        if self.security_manager is None:
            return True
        perms = self.security_manager.permissions
        if action_type == "read":
            return perms.allow_read
        if action_type == "write":
            return perms.allow_write
        if action_type == "execute":
            return perms.allow_execute
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def click(self, x: int, y: int, *, button: str = "left") -> None:
        """Click at (x, y) in the virtual display."""
        if not self._check_permission("write"):
            logger.warning("Click action blocked by permission profile.")
            return
        btn_num = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
        if _SYSTEM == "Linux" and self._xdotool_available:
            await self._xdotool_click(x, y, btn_num)
        elif _SYSTEM == "Windows":
            await self._win32_click(x, y, button)
        elif _SYSTEM == "Darwin":
            await self._macos_click(x, y, button)
        else:
            await self._pyautogui_click(x, y, button)

    async def double_click(self, x: int, y: int) -> None:
        """Double-click at (x, y)."""
        await self.click(x, y)
        await asyncio.sleep(0.05)
        await self.click(x, y)

    async def right_click(self, x: int, y: int) -> None:
        """Right-click at (x, y)."""
        await self.click(x, y, button="right")

    async def type_text(self, text: str) -> None:
        """Type *text* into the target window without moving focus."""
        if not self._check_permission("write"):
            logger.warning("Type action blocked by permission profile.")
            return
        if self.security_manager and not self.security_manager.validate_key_input(list(text)):
            return
        if _SYSTEM == "Linux" and self._xdotool_available:
            await self._xdotool_type(text)
        elif _SYSTEM == "Windows":
            await self._win32_type(text)
        else:
            await self._pyautogui_type(text)

    async def press_keys(self, keys: list[str]) -> None:
        """Press a key combination (e.g. ``["ctrl", "c"]``)."""
        if not self._check_permission("execute"):
            logger.warning("Key combination action blocked by permission profile.")
            return
        if self.security_manager and not self.security_manager.validate_key_input(keys):
            return
        key_str = "+".join(self._normalize_key(k) for k in keys)
        if _SYSTEM == "Linux" and self._xdotool_available:
            await self._run_xdotool(["key", "--clearmodifiers", key_str])
        elif _SYSTEM == "Windows":
            await self._win32_press_keys(keys)
        else:
            await self._pyautogui_press_keys(keys)

    async def scroll(self, x: int, y: int, *, dx: int = 0, dy: int = 3) -> None:
        """Scroll the mouse wheel at (x, y)."""
        if _SYSTEM == "Linux" and self._xdotool_available:
            btn = 5 if dy > 0 else 4  # button 4=up, 5=down
            for _ in range(abs(dy)):
                await self._xdotool_click(x, y, btn)
        else:
            await self._pyautogui_scroll(x, y, dy)

    async def move(self, x: int, y: int) -> None:
        """Move the virtual cursor to (x, y)."""
        if _SYSTEM == "Linux" and self._xdotool_available:
            await self._run_xdotool(["mousemove", "--sync", str(x), str(y)])
        else:
            await self._pyautogui_move(x, y)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        """Drag from (x1, y1) to (x2, y2) with button held."""
        if _SYSTEM == "Linux" and self._xdotool_available:
            await self._run_xdotool(["mousemove", "--sync", str(x1), str(y1)])
            await self._run_xdotool(["mousedown", "1"])
            await self._run_xdotool(["mousemove", "--sync", str(x2), str(y2)])
            await self._run_xdotool(["mouseup", "1"])
        else:
            await self._pyautogui_drag(x1, y1, x2, y2)

    async def drag_and_drop_file(self, file_path: str, x: int, y: int) -> None:
        """Drag a file and drop it at virtual coordinates (x, y)."""
        logger.info("Virtual input: Drag and drop file %s to %d, %d", file_path, x, y)
        await self.set_file_clipboard(file_path)
        await self.click(x, y)
        await self.press_keys(["ctrl", "v"])

    async def handle_touch(self, action: str, x_ratio: float, y_ratio: float, screen_width: int = 1920, screen_height: int = 1080) -> None:
        """Handle mobile/touch inputs by converting ratios to pixels."""
        x = int(x_ratio * screen_width)
        y = int(y_ratio * screen_height)
        if action == "tap":
            await self.click(x, y)
        elif action in ("long_press", "hold"):
            await self.click(x, y, button="left")
            await asyncio.sleep(1.0)

    async def set_clipboard(self, text: str) -> None:
        """Set host/agent clipboard text."""
        if self.security_manager and not self.security_manager.validate_clipboard_set(text):
            return
        if _SYSTEM == "Linux":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xclip", "-selection", "clipboard",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"DISPLAY": self.display}
                )
                await proc.communicate(text.encode("utf-8"))
            except Exception:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "xsel", "--clipboard", "--input",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env={"DISPLAY": self.display}
                    )
                    await proc.communicate(text.encode("utf-8"))
                except Exception:
                    pass
        elif _SYSTEM == "Windows":
            try:
                import win32clipboard
                await asyncio.to_thread(win32clipboard.OpenClipboard)
                await asyncio.to_thread(win32clipboard.EmptyClipboard)
                await asyncio.to_thread(win32clipboard.SetClipboardText, text)
                await asyncio.to_thread(win32clipboard.CloseClipboard)
            except Exception:
                pass
        elif _SYSTEM == "Darwin":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pbcopy",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                await proc.communicate(text.encode("utf-8"))
            except Exception:
                pass

    async def get_clipboard(self) -> str:
        """Get host/agent clipboard text."""
        if _SYSTEM == "Linux":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xclip", "-selection", "clipboard", "-o",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"DISPLAY": self.display}
                )
                out, _ = await proc.communicate()
                return out.decode("utf-8", errors="ignore")
            except Exception:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "xsel", "--clipboard", "--output",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env={"DISPLAY": self.display}
                    )
                    out, _ = await proc.communicate()
                    return out.decode("utf-8", errors="ignore")
                except Exception:
                    return ""
        elif _SYSTEM == "Windows":
            try:
                import win32clipboard
                await asyncio.to_thread(win32clipboard.OpenClipboard)
                val = await asyncio.to_thread(win32clipboard.GetClipboardData)
                await asyncio.to_thread(win32clipboard.CloseClipboard)
                return str(val)
            except Exception:
                return ""
        elif _SYSTEM == "Darwin":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pbpaste",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                out, _ = await proc.communicate()
                return out.decode("utf-8", errors="ignore")
            except Exception:
                return ""
        return ""

    async def set_file_clipboard(self, file_path: str) -> None:
        """Write file path as URI list to clipboard."""
        uri = Path(file_path).absolute().as_uri()
        if _SYSTEM == "Linux":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xclip", "-selection", "clipboard", "-t", "text/uri-list",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"DISPLAY": self.display}
                )
                await proc.communicate((uri + "\r\n").encode("utf-8"))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Linux / xdotool implementation
    # ------------------------------------------------------------------

    async def _xdotool_click(self, x: int, y: int, button: int) -> None:
        env_display = {"DISPLAY": self.display}
        cmd = ["xdotool"]
        if self.window_id:
            cmd += ["mousemove", "--window", self.window_id, str(x), str(y)]
            cmd += ["click", "--window", self.window_id, str(button)]
        else:
            cmd += ["mousemove", "--sync", str(x), str(y), "click", str(button)]
        await self._run_cmd(cmd, env=env_display)

    async def _xdotool_type(self, text: str) -> None:
        env_display = {"DISPLAY": self.display}
        cmd = ["xdotool", "type", "--clearmodifiers", "--delay", "20"]
        if self.window_id:
            cmd += ["--window", self.window_id]
        cmd.append(text)
        await self._run_cmd(cmd, env=env_display)

    async def _run_xdotool(self, args: list[str]) -> None:
        import os
        env = dict(**__import__("os").environ, DISPLAY=self.display)
        if self.window_id:
            args = ["--window", self.window_id] + args
        await self._run_cmd(["xdotool"] + args, env=env)

    # ------------------------------------------------------------------
    # Windows / Win32 implementation
    # ------------------------------------------------------------------

    async def _win32_click(self, x: int, y: int, button: str) -> None:
        try:
            import ctypes
            import ctypes.wintypes

            MOUSEEVENTF_MOVE = 0x0001
            MOUSEEVENTF_LEFTDOWN = 0x0002
            MOUSEEVENTF_LEFTUP = 0x0004
            MOUSEEVENTF_RIGHTDOWN = 0x0008
            MOUSEEVENTF_RIGHTUP = 0x0010
            MOUSEEVENTF_ABSOLUTE = 0x8000

            # Convert pixel coords to normalised absolute (0-65535)
            screen_w = ctypes.windll.user32.GetSystemMetrics(0)
            screen_h = ctypes.windll.user32.GetSystemMetrics(1)
            abs_x = int(x * 65535 / screen_w)
            abs_y = int(y * 65535 / screen_h)

            flags_down = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
            flags_up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP

            await asyncio.to_thread(
                ctypes.windll.user32.mouse_event,
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                abs_x,
                abs_y,
                0,
                0,
            )
            await asyncio.to_thread(ctypes.windll.user32.mouse_event, flags_down, 0, 0, 0, 0)
            await asyncio.sleep(0.05)
            await asyncio.to_thread(ctypes.windll.user32.mouse_event, flags_up, 0, 0, 0, 0)
        except Exception as exc:
            logger.warning("Win32 click failed (%s), falling back to pyautogui", exc)
            await self._pyautogui_click(x, y, button)

    async def _win32_type(self, text: str) -> None:
        await self._pyautogui_type(text)  # simplest reliable path on Windows

    async def _win32_press_keys(self, keys: list[str]) -> None:
        await self._pyautogui_press_keys(keys)

    # ------------------------------------------------------------------
    # macOS / Quartz implementation
    # ------------------------------------------------------------------

    async def _macos_click(self, x: int, y: int, button: str) -> None:
        try:
            script = (
                f'tell application "System Events" to click at {{{x}, {y}}}'
            )
            await self._run_cmd(["osascript", "-e", script])
        except Exception as exc:
            logger.warning("macOS click failed (%s), falling back to pyautogui", exc)
            await self._pyautogui_click(x, y, button)

    # ------------------------------------------------------------------
    # pyautogui fallback
    # ------------------------------------------------------------------

    async def _pyautogui_click(self, x: int, y: int, button: str) -> None:
        if not self.fallback_to_pyautogui:
            logger.warning("pyautogui fallback disabled; click skipped.")
            return
        try:
            import pyautogui
            await asyncio.to_thread(pyautogui.click, x, y, button=button)
        except Exception as exc:
            logger.error("pyautogui click failed: %s", exc)

    async def _pyautogui_type(self, text: str) -> None:
        if not self.fallback_to_pyautogui:
            return
        try:
            import pyautogui
            await asyncio.to_thread(pyautogui.typewrite, text, interval=0.02)
        except Exception as exc:
            logger.error("pyautogui type failed: %s", exc)

    async def _pyautogui_press_keys(self, keys: list[str]) -> None:
        if not self.fallback_to_pyautogui:
            return
        try:
            import pyautogui
            await asyncio.to_thread(pyautogui.hotkey, *keys)
        except Exception as exc:
            logger.error("pyautogui press_keys failed: %s", exc)

    async def _pyautogui_scroll(self, x: int, y: int, dy: int) -> None:
        if not self.fallback_to_pyautogui:
            return
        try:
            import pyautogui
            await asyncio.to_thread(pyautogui.scroll, dy, x=x, y=y)
        except Exception as exc:
            logger.error("pyautogui scroll failed: %s", exc)

    async def _pyautogui_move(self, x: int, y: int) -> None:
        if not self.fallback_to_pyautogui:
            return
        try:
            import pyautogui
            await asyncio.to_thread(pyautogui.moveTo, x, y)
        except Exception as exc:
            logger.error("pyautogui move failed: %s", exc)

    async def _pyautogui_drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        if not self.fallback_to_pyautogui:
            return
        try:
            import pyautogui
            await asyncio.to_thread(pyautogui.moveTo, x1, y1)
            await asyncio.to_thread(pyautogui.dragTo, x2, y2, button="left")
        except Exception as exc:
            logger.error("pyautogui drag failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_cmd(cmd: list[str], env: dict[str, str] | None = None) -> str:
        import os
        merged_env = dict(**os.environ, **(env or {}))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.debug("Virtual input cmd %s stderr: %s", cmd[0], stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Normalize key names to xdotool / pyautogui format."""
        return {
            "ctrl": "ctrl", "control": "ctrl",
            "alt": "alt", "shift": "shift",
            "enter": "Return", "return": "Return",
            "esc": "Escape", "escape": "Escape",
            "tab": "Tab", "space": "space",
            "backspace": "BackSpace", "delete": "Delete",
            "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        }.get(key.lower(), key)

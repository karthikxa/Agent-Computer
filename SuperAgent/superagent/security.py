"""Security, Data Loss Prevention (DLP), and Watermarking engine for SuperAgent.

Implements clipboard rate-limiting, keystroke logging, watermark overlays,
and runtime permission profiles for multi-agent safety.
"""

from __future__ import annotations

import io
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PermissionProfile:
    """Read/Write/Execute permission schema for an agent."""

    allow_read: bool = True
    allow_write: bool = True
    allow_execute: bool = True


@dataclass
class SecurityConfig:
    """Security and Data Loss Prevention configs."""

    enable_https: bool = False
    log_keystrokes: bool = True
    log_clipboard: bool = True
    max_clipboard_size: int = 10 * 1024  # 10 KB
    min_clipboard_interval: float = 1.0   # 1 second
    keyboard_rate_limit: float = 0.05    # seconds between keys (max 20 keys/sec)
    enable_watermark: bool = True
    watermark_text: str = "CONFIDENTIAL - SUPERAGENT"
    session_timeout_seconds: float = 300.0  # 5 minutes
    screen_restriction_rect: tuple[int, int, int, int] | None = None  # (x, y, w, h)


class SecurityManager:
    """Handles DLP auditing, clipboard validation, watermarking, and timeouts."""

    def __init__(self, config: SecurityConfig | None = None, permission_profile: PermissionProfile | None = None, audit_log_path: str = ".superagent/audit.log") -> None:
        self.config = config or SecurityConfig()
        self.permissions = permission_profile or PermissionProfile()
        self.audit_log_path = Path(audit_log_path)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._last_clipboard_time = 0.0
        self._last_key_time = 0.0
        self._last_activity_time = time.monotonic()

    def log_action(self, event_type: str, details: str) -> None:
        """Write a secure log entry to the audit trail."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] [{event_type}] {details}\n"
        with open(self.audit_log_path, "a", encoding="utf-8") as f:
            f.write(entry)

    # --- Clipboard validation ---
    def validate_clipboard_set(self, text: str) -> bool:
        """Enforce size limits and time spacing between clipboard operations."""
        now = time.monotonic()
        if len(text.encode("utf-8")) > self.config.max_clipboard_size:
            logger.warning("Clipboard transfer blocked: content exceeds max size limit.")
            self.log_action("CLIPBOARD_BLOCKED", f"Size check failed: {len(text)} bytes")
            return False

        if now - self._last_clipboard_time < self.config.min_clipboard_interval:
            logger.warning("Clipboard transfer blocked: rate limit exceeded.")
            self.log_action("CLIPBOARD_BLOCKED", "Rate limit check failed")
            return False

        self._last_clipboard_time = now
        if self.config.log_clipboard:
            self.log_action("CLIPBOARD_SET", text[:200])
        return True

    # --- Keyboard rate-limiting & logging ---
    def validate_key_input(self, keys: list[str]) -> bool:
        """Rate-limit keystrokes and log keyboard inputs."""
        now = time.monotonic()
        self._last_activity_time = now
        
        if now - self._last_key_time < self.config.keyboard_rate_limit:
            logger.warning("Keyboard input rate limited.")
            return False

        self._last_key_time = now
        if self.config.log_keystrokes:
            self.log_action("KEYSTROKE", ",".join(keys))
        return True

    # --- Watermarking overlay ---
    def apply_watermark(self, png_bytes: bytes) -> bytes:
        """Apply a dynamic text-based watermark with rotating timestamp onto screenshot."""
        if not self.config.enable_watermark:
            return png_bytes

        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.open(io.BytesIO(png_bytes))
            draw = ImageDraw.Draw(img)
            
            # Timestamp rotation
            watermark = f"{self.config.watermark_text} | {time.strftime('%Y-%m-%d %H:%M:%S')}"
            
            # Draw at the bottom-right corner or diagonally
            width, height = img.size
            draw.text((20, height - 40), watermark, fill=(255, 0, 0, 128))
            
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            logger.debug("Failed to apply watermark via PIL: %s", e)
            return png_bytes

    # --- Session timeout ---
    def check_inactivity_timeout(self) -> bool:
        """Check if the session has timed out due to inactivity."""
        if time.monotonic() - self._last_activity_time > self.config.session_timeout_seconds:
            self.log_action("TIMEOUT", "Session auto-timeout due to inactivity")
            return True
        return False

    def reset_activity(self) -> None:
        self._last_activity_time = time.monotonic()

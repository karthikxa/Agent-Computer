"""Async client for the container desktop API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import aiohttp


class DesktopConnectionError(RuntimeError):
    """Raised when the desktop API is unreachable."""


@dataclass(slots=True)
class DesktopState:
    connected: bool = False
    resolution: tuple[int, int] = (3840, 2160)
    metadata: dict[str, Any] | None = None


class DesktopAPI:
    """Real HTTP client for the desktop API."""

    def __init__(self, host: str = "localhost", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        timeout = aiohttp.ClientTimeout(total=30)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, f"{self.base_url}{path}", json=payload) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            raise RuntimeError(f"{resp.status}: {text}")
                        if path == "/screenshot":
                            return await resp.read()
                        if resp.headers.get("content-type", "").startswith("application/json"):
                            return await resp.json()
                        return await resp.text()
            except (aiohttp.ClientConnectionError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1)
        raise DesktopConnectionError(
            f"Cannot connect to desktop API at {self.base_url}. Is the Docker container running?"
        ) from last_error

    async def screenshot(self) -> bytes:
        return await self._request("GET", "/screenshot")

    async def click(self, x: int, y: int) -> Any:
        return await self._request("POST", "/click", {"x": x, "y": y})

    async def double_click(self, x: int, y: int) -> Any:
        return await self._request("POST", "/double_click", {"x": x, "y": y})

    async def type_text(self, text: str) -> Any:
        return await self._request("POST", "/type", {"text": text})

    async def press_keys(self, keys: str) -> Any:
        return await self._request("POST", "/key", {"keys": keys})

    async def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3) -> Any:
        return await self._request("POST", "/scroll", {"x": x, "y": y, "direction": direction, "amount": amount})

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> Any:
        return await self._request("POST", "/drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def run_command(self, command: str) -> Any:
        return await self._request("POST", "/command", {"cmd": command})

    async def get_screen_size(self) -> dict[str, int]:
        return await self._request("GET", "/screen_size")

    async def health(self) -> Any:
        return await self._request("GET", "/health")

    async def close(self) -> None:
        return None


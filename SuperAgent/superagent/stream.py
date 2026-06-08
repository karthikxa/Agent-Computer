"""Stream management for KasmVNC and HLS fallback."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StreamConfig:
    """Stream configuration values."""

    host: str = "127.0.0.1"
    vnc_port: int = 6901
    hls_port: int = 7080
    stream_dir: Path = Path("/tmp/agent-stream")
    display: str = ":1"
    resolution_4k: str = "3840x2160"
    resolution_1080p: str = "1920x1080"


class StreamManager:
    """Manage browser-based access to the desktop."""

    def __init__(self, config: StreamConfig | None = None) -> None:
        self.config = config or StreamConfig()
        self._processes: list[subprocess.Popen[Any]] = []
        self._started = False
        self._mode = "unknown"

    async def start(self) -> None:
        """Detect the available stream and start a fallback when needed."""

        self.config.stream_dir.mkdir(parents=True, exist_ok=True)
        if self._started:
            return
        await self.auto_detect()

    async def stop(self) -> None:
        """Stop managed processes."""

        for process in self._processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self._processes.clear()
        self._started = False

    async def auto_detect(self) -> str:
        """
        Check if KasmVNC is running on port 6901 first.
        Fall back to FFmpeg HLS on port 7080 if not available.
        Returns the stream URL that is actually available.
        """
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{self.config.host}:6901",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    if resp.status < 500:
                        self._mode = "kasmvnc"
                        self._started = True
                        return f"http://{self.config.host}:6901"
        except Exception:
            pass

        self._mode = "ffmpeg"
        self.start_ffmpeg_fallback()
        self._started = True
        return f"http://{self.config.host}:7080/index.m3u8"

    def start_ffmpeg_fallback(self) -> None:
        """Start FFmpeg H.264 HLS stream as fallback when KasmVNC unavailable."""
        self.config.stream_dir.mkdir(parents=True, exist_ok=True)
        self._processes.append(
            subprocess.Popen(
                [
                    "ffmpeg",
                    "-f",
                    "x11grab",
                    "-r",
                    "30",
                    "-s",
                    "3840x2160",
                    "-i",
                    ":1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-b:v",
                    "8000k",
                    "-maxrate",
                    "8000k",
                    "-bufsize",
                    "16000k",
                    "-f",
                    "hls",
                    "-hls_time",
                    "0.5",
                    "-hls_list_size",
                    "6",
                    "-hls_flags",
                    "delete_segments",
                    str(self.config.stream_dir / "index.m3u8"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        self._processes.append(
            subprocess.Popen(
                [
                    "python3",
                    "-m",
                    "http.server",
                    "7080",
                    "--directory",
                    str(self.config.stream_dir),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    def get_url(self, auth_key: str | None = None) -> str:
        """Return the primary KasmVNC/WebRTC URL."""

        query = f"?token={auth_key}" if auth_key else ""
        return f"http://{self.config.host}:{self.config.vnc_port}/{query}"

    def get_4k_url(self, auth_key: str | None = None) -> str:
        """Return the high-resolution stream URL."""

        query = f"?token={auth_key}&quality=4k" if auth_key else "?quality=4k"
        return f"http://{self.config.host}:{self.config.vnc_port}/{query}"

    def get_1080p_url(self, auth_key: str | None = None) -> str:
        """Return the lower-bandwidth stream URL."""

        query = f"?token={auth_key}&quality=1080p" if auth_key else "?quality=1080p"
        return f"http://{self.config.host}:{self.config.vnc_port}/{query}"

    def get_hls_url(self, auth_key: str | None = None) -> str:
        """Return the fallback HLS manifest URL."""

        query = f"?token={auth_key}" if auth_key else ""
        return f"http://{self.config.host}:{self.config.hls_port}/index.m3u8{query}"

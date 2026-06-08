"""Stream management for KasmVNC, HLS fallback, and WebP frame streaming.

KasmVNC uses modern codecs (WebP/QOI) for high-efficiency streaming.
SuperAgent now mirrors that approach with:
  - Primary   : KasmVNC on port 6901 (WebSocket/HTTPS, browser-native)
  - WebP feed : Lightweight MJPEG-over-HTTP with WebP frames on port 7081
  - HLS       : H.264 FFmpeg fallback on port 7080
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamConfig:
    """Stream configuration values."""

    host: str = "127.0.0.1"
    vnc_port: int = 6901
    hls_port: int = 7080
    webp_port: int = 7081          # WebP frame-push server port
    webrtc_port: int = 7082        # WebRTC signaling port
    qoi_port: int = 7083           # QOI streaming port
    stream_dir: Path = Path("/tmp/agent-stream")
    display: str = ":1"
    resolution_4k: str = "3840x2160"
    resolution_1080p: str = "1920x1080"
    webp_quality: int = 80         # WebP compression quality (0-100)
    webp_fps: int = 10             # WebP frame capture rate
    enable_webrtc: bool = True
    enable_qoi: bool = True


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

    def get_webp_url(self, auth_key: str | None = None) -> str:
        """Return the WebP frame-push stream URL (new KasmVNC-inspired codec)."""

        query = f"?token={auth_key}" if auth_key else ""
        return f"http://{self.config.host}:{self.config.webp_port}/stream{query}"

    # ------------------------------------------------------------------
    # WebP, QOI, and WebRTC streaming (KasmVNC-inspired modern codecs)
    # ------------------------------------------------------------------

    @staticmethod
    def encode_webp(png_bytes: bytes, quality: int = 80) -> bytes:
        """Re-encode a PNG screenshot to WebP for bandwidth-efficient streaming."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=quality, method=4)
            return buf.getvalue()
        except Exception:
            return png_bytes

    @staticmethod
    def encode_qoi(png_bytes: bytes) -> bytes:
        """Encode PNG screenshot bytes to Quite OK Image Format (QOI)."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            width, height = img.size
            pixels = img.tobytes()
        except Exception:
            return png_bytes

        out = bytearray()
        out.extend(b"qoif")
        out.extend(width.to_bytes(4, "big"))
        out.extend(height.to_bytes(4, "big"))
        out.append(4)
        out.append(0)

        index = [[0, 0, 0, 0] for _ in range(64)]
        px_r, px_g, px_b, px_a = 0, 0, 0, 255
        run = 0
        num_pixels = width * height

        for i in range(num_pixels):
            offset = i * 4
            r = pixels[offset]
            g = pixels[offset+1]
            b = pixels[offset+2]
            a = pixels[offset+3]

            if r == px_r and g == px_g and b == px_b and a == px_a:
                run += 1
                if run == 62 or i == num_pixels - 1:
                    out.append(0xc0 | (run - 1))
                    run = 0
            else:
                if run > 0:
                    out.append(0xc0 | (run - 1))
                    run = 0

                index_pos = (r * 3 + g * 5 + b * 7 + a * 11) % 64
                if index[index_pos] == [r, g, b, a]:
                    out.append(0x00 | index_pos)
                else:
                    index[index_pos] = [r, g, b, a]
                    if a == px_a:
                        vr = r - px_r
                        vg = g - px_g
                        vb = b - px_b
                        vg_co = vg + 32
                        vr_co = vr - vg + 8
                        vb_co = vb - vg + 8
                        if -2 <= vr <= 1 and -2 <= vg <= 1 and -2 <= vb <= 1:
                            out.append(0x40 | ((vr + 2) << 4) | ((vg + 2) << 2) | (vb + 2))
                        elif 0 <= vg_co <= 63 and 0 <= vr_co <= 15 and 0 <= vb_co <= 15:
                            out.append(0x80 | vg_co)
                            out.append((vr_co << 4) | vb_co)
                        else:
                            out.append(0xfe)
                            out.append(r)
                            out.append(g)
                            out.append(b)
                    else:
                        out.append(0xff)
                        out.append(r)
                        out.append(g)
                        out.append(b)
                        out.append(a)

            px_r, px_g, px_b, px_a = r, g, b, a

        out.extend(b"\x00\x00\x00\x00\x00\x00\x00\x01")
        return bytes(out)

    async def start_webp_stream(self, desktop_api: Any) -> None:
        """Start a unified HTTP/WS server hosting WebP, QOI streams and WebRTC signaling."""
        try:
            from aiohttp import web
        except ImportError:
            logger.warning("aiohttp not installed — Advanced stream servers disabled.")
            return

        config = self.config
        import json

        async def _webp_handler(request: Any) -> Any:
            response = web.StreamResponse(
                headers={
                    "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                }
            )
            await response.prepare(request)
            while True:
                try:
                    png = await desktop_api.screenshot()
                    webp = await asyncio.to_thread(
                        StreamManager.encode_webp, png, config.webp_quality
                    )
                    frame = (
                        b"--frame\r\n"
                        b"Content-Type: image/webp\r\n"
                        b"Content-Length: " + str(len(webp)).encode() + b"\r\n\r\n"
                        + webp + b"\r\n"
                    )
                    await response.write(frame)
                    await asyncio.sleep(1.0 / config.webp_fps)
                except Exception:
                    break
            return response

        async def _qoi_handler(request: Any) -> Any:
            response = web.StreamResponse(
                headers={
                    "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                }
            )
            await response.prepare(request)
            while True:
                try:
                    png = await desktop_api.screenshot()
                    qoi = await asyncio.to_thread(StreamManager.encode_qoi, png)
                    frame = (
                        b"--frame\r\n"
                        b"Content-Type: image/x-qoi\r\n"
                        b"Content-Length: " + str(len(qoi)).encode() + b"\r\n\r\n"
                        + qoi + b"\r\n"
                    )
                    await response.write(frame)
                    await asyncio.sleep(1.0 / config.webp_fps)
                except Exception:
                    break
            return response

        async def _webrtc_handler(request: Any) -> Any:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        # Broadcast or loopback WebRTC SDP signalling messages
                        await ws.send_json(data)
                    except Exception as e:
                        logger.error("WebRTC Signaling error: %s", e)
            return ws

        app = web.Application()
        app.router.add_get("/stream", _webp_handler)
        app.router.add_get("/qoi", _qoi_handler)
        app.router.add_get("/webrtc", _webrtc_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.host, config.webp_port)
        await site.start()
        self._processes.append(runner)
        logger.info(
            "Unified stream server (WebP/QOI/WebRTC) started at http://%s:%d/",
            config.host, config.webp_port
        )

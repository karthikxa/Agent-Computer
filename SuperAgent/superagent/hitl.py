"""Human-in-the-Loop (HITL) HTTP control server for SuperAgent.

Inspired by e2b-dev/open-computer-use interactive pause/resume feature.

Exposes a lightweight aiohttp REST API so an operator (or a front-end UI)
can pause, resume, inject instructions, and inspect the agent's live status
— all without modifying the agent loop code.

Endpoints
---------
GET  /status           → current loop state (step, paused, objective)
POST /pause            → pause the agent loop
POST /resume           → resume a paused loop
POST /inject           → inject an instruction  (body: {"instruction": "..."})
POST /stop             → request a graceful stop
GET  /screenshot       → latest desktop screenshot as base64 PNG
GET  /stream_url       → current KasmVNC / HLS URL
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    from aiohttp import web as _web
    _AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    _AIOHTTP_AVAILABLE = False
    _web = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# HITL server
# ---------------------------------------------------------------------------

@dataclass
class HITLServer:
    """Async HTTP server wiring human operators to a live SuperAgent loop.

    Usage::

        hitl = HITLServer(agent=my_superagent, port=9000)
        asyncio.create_task(hitl.start())
        # open http://localhost:9000/status in a browser
    """

    agent: Any = None
    host: str = "127.0.0.1"
    port: int = 9000
    _runner: Any = None  # aiohttp AppRunner

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not _AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not installed — HITL server disabled.")
            return
        app = _web.Application()
        app.router.add_get("/status", self._handle_status)
        app.router.add_post("/pause", self._handle_pause)
        app.router.add_post("/resume", self._handle_resume)
        app.router.add_post("/inject", self._handle_inject)
        app.router.add_post("/stop", self._handle_stop)
        app.router.add_get("/screenshot", self._handle_screenshot)
        app.router.add_get("/stream_url", self._handle_stream_url)
        app.router.add_post("/permissions", self._handle_permissions)
        app.router.add_post("/copilot", self._handle_copilot)
        self._runner = _web.AppRunner(app)
        await self._runner.setup()
        site = _web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("HITL server listening on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            logger.info("HITL server stopped")

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    async def _handle_status(self, request: Any) -> Any:
        loop = self._get_loop()
        if loop is None:
            return _web.Response(
                content_type="application/json",
                text=json.dumps({"error": "no agent connected"}),
                status=503,
            )
        state = loop.state
        return _web.Response(
            content_type="application/json",
            text=json.dumps({
                "paused": state.paused,
                "done": state.done,
                "step_count": state.step_count,
                "objective": state.objective,
                "instructions_pending": len(state.instructions),
            }),
        )

    async def _handle_pause(self, request: Any) -> Any:
        loop = self._get_loop()
        if loop is None:
            return _web.Response(status=503, text="no agent")
        loop.pause()
        logger.info("HITL: agent paused via HTTP")
        return _web.Response(content_type="application/json", text=json.dumps({"paused": True}))

    async def _handle_resume(self, request: Any) -> Any:
        loop = self._get_loop()
        if loop is None:
            return _web.Response(status=503, text="no agent")
        loop.resume()
        logger.info("HITL: agent resumed via HTTP")
        return _web.Response(content_type="application/json", text=json.dumps({"paused": False}))

    async def _handle_inject(self, request: Any) -> Any:
        loop = self._get_loop()
        if loop is None:
            return _web.Response(status=503, text="no agent")
        try:
            body = await request.json()
            instruction = body.get("instruction", "").strip()
        except Exception:
            return _web.Response(status=400, text="invalid JSON body")
        if not instruction:
            return _web.Response(status=400, text="instruction required")
        loop.inject_instruction(instruction)
        logger.info("HITL: injected instruction: %r", instruction)
        return _web.Response(
            content_type="application/json",
            text=json.dumps({"injected": instruction}),
        )

    async def _handle_stop(self, request: Any) -> Any:
        loop = self._get_loop()
        if loop is None:
            return _web.Response(status=503, text="no agent")
        loop.inject_instruction("STOP: human operator requested graceful stop.")
        loop.pause()
        return _web.Response(content_type="application/json", text=json.dumps({"stopping": True}))

    async def _handle_screenshot(self, request: Any) -> Any:
        desktop = getattr(self.agent, "desktop_api", None)
        if desktop is None:
            return _web.Response(status=503, text="no desktop_api connected")
        try:
            data = await desktop.screenshot()
            b64 = base64.b64encode(data).decode()
            return _web.Response(
                content_type="application/json",
                text=json.dumps({"screenshot_b64": b64, "format": "png"}),
            )
        except Exception as exc:
            return _web.Response(status=500, text=str(exc))

    async def _handle_stream_url(self, request: Any) -> Any:
        stream = getattr(self.agent, "stream", None)
        if stream is None:
            return _web.Response(status=503, text="no stream manager")
        return _web.Response(
            content_type="application/json",
            text=json.dumps({
                "vnc_url": stream.get_url(),
                "hls_url": stream.get_hls_url(),
                "4k_url": stream.get_4k_url(),
            }),
        )

    async def _handle_permissions(self, request: Any) -> Any:
        try:
            body = await request.json()
        except Exception:
            return _web.Response(status=400, text="invalid JSON")
        sec_mgr = getattr(self.agent, "security_manager", None)
        if sec_mgr is None:
            return _web.Response(status=503, text="no security manager configured")
        
        perms = sec_mgr.permissions
        if "allow_read" in body:
            perms.allow_read = bool(body["allow_read"])
        if "allow_write" in body:
            perms.allow_write = bool(body["allow_write"])
        if "allow_execute" in body:
            perms.allow_execute = bool(body["allow_execute"])
        
        logger.info("HITL: updated agent permissions to read=%s, write=%s, execute=%s",
                    perms.allow_read, perms.allow_write, perms.allow_execute)
        return _web.Response(
            content_type="application/json",
            text=json.dumps({
                "allow_read": perms.allow_read,
                "allow_write": perms.allow_write,
                "allow_execute": perms.allow_execute
            })
        )

    async def _handle_copilot(self, request: Any) -> Any:
        try:
            body = await request.json()
            action = body.get("action", "").strip()
        except Exception:
            return _web.Response(status=400, text="invalid JSON")
        
        loop = self._get_loop()
        if loop is None:
            return _web.Response(status=503, text="no loop connected")
        
        if action == "takeover":
            loop.pause()
            logger.info("HITL: Co-pilot co-piloting takeover started (agent paused)")
            return _web.Response(content_type="application/json", text=json.dumps({"copilot": "active", "agent_paused": True}))
        elif action == "release":
            loop.resume()
            logger.info("HITL: Co-pilot takeover released (agent resumed)")
            return _web.Response(content_type="application/json", text=json.dumps({"copilot": "inactive", "agent_paused": False}))
        else:
            return _web.Response(status=400, text="action must be 'takeover' or 'release'")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_loop(self) -> Any:
        return getattr(self.agent, "loop", None)

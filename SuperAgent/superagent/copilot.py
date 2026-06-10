"""Co-pilot mode — human operator desktop takeover.

Feature #71 — inspired by KasmVNC co-pilot / control handoff.

Allows a human operator to:
  1. VIEW any agent desktop live (read-only stream proxy)
  2. TAKE OVER the agent's input (co-pilot mode — operator controls mouse/keyboard)
  3. HAND BACK control to the agent after reviewing

The co-pilot mode pauses the agent loop and injects the operator's
input events (forwarded from the dashboard WebSocket) directly into
the agent's VirtualInputDriver.

Architecture
------------
  Dashboard WS ──► CoPilotSession ──► VirtualInputDriver (agent desktop)
                        │
                        └─► AgentLoop.pause() / .resume()

Usage::

    copilot = CoPilotServer(agent=agent_instance)
    asyncio.create_task(copilot.start())   # listens on port 9300

    # Dashboard connects:  ws://host:9300/copilot/{agent_id}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9300


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

@dataclass
class CoPilotSession:
    """Represents one active co-pilot handoff session."""

    session_id: str
    agent_id: str
    operator_id: str
    started_at: float = field(default_factory=time.time)
    is_active: bool = True
    mode: str = "view"   # "view" | "control"
    events_sent: int = 0


# ---------------------------------------------------------------------------
# CoPilot Server
# ---------------------------------------------------------------------------

class CoPilotServer:
    """WebSocket server that lets operators view or take over agent desktops.

    Endpoints
    ---------
    GET /copilot/{agent_id}/view     — stream screenshots (read-only)
    GET /copilot/{agent_id}/control  — full input injection (control mode)
    GET /copilot/sessions            — list all active sessions
    POST /copilot/{agent_id}/release — hand control back to agent
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = _DEFAULT_PORT,
    ) -> None:
        self.host = host
        self.port = port
        # agent_id → (agent_obj, loop_obj, virtual_input_obj)
        self._agents: dict[str, tuple[Any, Any, Any]] = {}
        self._sessions: dict[str, CoPilotSession] = {}
        self._runner: Any = None

    def register_agent(self, agent_id: str, agent: Any, loop: Any, virtual_input: Any) -> None:
        """Register an agent so operators can connect to it."""
        self._agents[agent_id] = (agent, loop, virtual_input)
        logger.debug("CoPilot: registered agent %s", agent_id)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        try:
            from aiohttp import web
            app = web.Application()
            app.router.add_get("/copilot/{agent_id}/view",    self._handle_view)
            app.router.add_get("/copilot/{agent_id}/control", self._handle_control)
            app.router.add_get("/copilot/sessions",            self._handle_sessions)
            app.router.add_post("/copilot/{agent_id}/release", self._handle_release)

            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.host, self.port)
            await site.start()
            logger.info("CoPilot server started at ws://%s:%d/copilot/", self.host, self.port)
        except ImportError:
            logger.warning("aiohttp not installed — CoPilot server disabled")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # WebSocket handlers
    # ------------------------------------------------------------------

    async def _handle_view(self, request: Any) -> Any:
        """Read-only screenshot stream for dashboard viewing (feature #70 + #71)."""
        from aiohttp import web, WSMsgType
        agent_id = request.match_info["agent_id"]
        agent_tuple = self._agents.get(agent_id)

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session_id = self._make_session_id()
        session = CoPilotSession(
            session_id=session_id,
            agent_id=agent_id,
            operator_id=request.headers.get("X-Operator-ID", "anonymous"),
            mode="view",
        )
        self._sessions[session_id] = session
        logger.info("CoPilot: view session started for agent=%s", agent_id)

        try:
            while not ws.closed:
                # Stream screenshots at ~5 fps
                if agent_tuple:
                    _, _, vi = agent_tuple
                    desktop_api = getattr(vi, "_desktop_api", None)
                    if desktop_api:
                        try:
                            png = await desktop_api.screenshot()
                            import base64
                            await ws.send_json({
                                "type": "screenshot",
                                "data": base64.b64encode(png).decode(),
                                "timestamp": time.time(),
                            })
                        except Exception:
                            pass
                await asyncio.sleep(0.2)
        finally:
            self._sessions.pop(session_id, None)
        return ws

    async def _handle_control(self, request: Any) -> Any:
        """Full control mode — operator sends input events, agent is paused."""
        from aiohttp import web, WSMsgType
        agent_id = request.match_info["agent_id"]
        agent_tuple = self._agents.get(agent_id)

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session_id = self._make_session_id()
        session = CoPilotSession(
            session_id=session_id,
            agent_id=agent_id,
            operator_id=request.headers.get("X-Operator-ID", "anonymous"),
            mode="control",
        )
        self._sessions[session_id] = session
        logger.info("CoPilot: CONTROL session started for agent=%s", agent_id)

        # Pause the agent loop
        if agent_tuple:
            _, loop, _ = agent_tuple
            if loop and hasattr(loop, "pause"):
                loop.pause()
                logger.info("CoPilot: agent %s loop PAUSED for takeover", agent_id)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch_input(agent_id, agent_tuple, msg.data)
                    session.events_sent += 1
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            # Resume agent loop on disconnect
            if agent_tuple:
                _, loop, _ = agent_tuple
                if loop and hasattr(loop, "resume"):
                    loop.resume()
                    logger.info("CoPilot: agent %s loop RESUMED after takeover", agent_id)
            self._sessions.pop(session_id, None)
        return ws

    async def _handle_sessions(self, request: Any) -> Any:
        """List all active co-pilot sessions."""
        from aiohttp import web
        sessions = [
            {
                "session_id": s.session_id,
                "agent_id": s.agent_id,
                "operator_id": s.operator_id,
                "mode": s.mode,
                "started_at": s.started_at,
                "events_sent": s.events_sent,
            }
            for s in self._sessions.values()
        ]
        return web.Response(
            content_type="application/json",
            text=json.dumps({"sessions": sessions}),
        )

    async def _handle_release(self, request: Any) -> Any:
        """Release control — resume agent loop."""
        from aiohttp import web
        agent_id = request.match_info["agent_id"]
        agent_tuple = self._agents.get(agent_id)
        if agent_tuple:
            _, loop, _ = agent_tuple
            if loop and hasattr(loop, "resume"):
                loop.resume()
        # Close control sessions for this agent
        to_remove = [sid for sid, s in self._sessions.items()
                     if s.agent_id == agent_id and s.mode == "control"]
        for sid in to_remove:
            self._sessions.pop(sid, None)
        return web.Response(
            content_type="application/json",
            text=json.dumps({"released": agent_id}),
        )

    # ------------------------------------------------------------------
    # Input dispatching
    # ------------------------------------------------------------------

    async def _dispatch_input(
        self, agent_id: str, agent_tuple: Any, raw: str
    ) -> None:
        """Parse an operator input event and inject it into the virtual input."""
        if not agent_tuple:
            return
        _, _, virtual_input = agent_tuple
        if not virtual_input:
            return
        try:
            event = json.loads(raw)
            etype = event.get("type", "")
            if etype == "click":
                await virtual_input.click(event["x"], event["y"], button=event.get("button", "left"))
            elif etype == "double_click":
                await virtual_input.double_click(event["x"], event["y"])
            elif etype == "right_click":
                await virtual_input.right_click(event["x"], event["y"])
            elif etype == "type":
                await virtual_input.type_text(event["text"])
            elif etype == "key":
                await virtual_input.press_keys(event["keys"])
            elif etype == "scroll":
                await virtual_input.scroll(event["x"], event["y"], dy=event.get("dy", 3))
            elif etype == "drag":
                await virtual_input.drag(event["x1"], event["y1"], event["x2"], event["y2"])
            elif etype == "move":
                await virtual_input.move(event["x"], event["y"])
            else:
                logger.debug("CoPilot: unknown event type '%s'", etype)
        except Exception as exc:
            logger.warning("CoPilot: failed to dispatch input event: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_session_id() -> str:
        import secrets
        return secrets.token_hex(8)

    def list_sessions(self) -> list[CoPilotSession]:
        return list(self._sessions.values())

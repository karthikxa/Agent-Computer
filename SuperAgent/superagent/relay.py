"""Unix relay for bidirectional agent-to-dashboard communication.

Feature #89 — inspired by KasmVNC's relay mechanism.

Provides a Unix domain socket relay that forwards messages between:
  - Agent processes (producers of status/events)
  - Dashboard WebSocket clients (consumers)

Architecture
------------
  AgentProcess → UnixSocket(:///tmp/superagent-relay.sock) → RelayServer → WebSocket Clients

Usage::

    relay = RelayServer()
    asyncio.create_task(relay.run())

    # From agent side:
    await relay.publish("agent-1", {"type": "status", "step": 5})

    # Dashboard connects via WebSocket ws://host:9200/ws
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_SOCKET = "/tmp/superagent-relay.sock"
_DEFAULT_WS_PORT = 9200


# ---------------------------------------------------------------------------
# Message model
# ---------------------------------------------------------------------------

@dataclass
class RelayMessage:
    """A message flowing through the relay."""

    agent_id: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "agent_id": self.agent_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
        })

    @classmethod
    def from_json(cls, data: str) -> "RelayMessage":
        d = json.loads(data)
        return cls(
            agent_id=d["agent_id"],
            payload=d["payload"],
            timestamp=d.get("timestamp", time.time()),
        )


# ---------------------------------------------------------------------------
# Relay Server
# ---------------------------------------------------------------------------

class RelayServer:
    """Bridges Unix socket messages to WebSocket dashboard clients.

    - Listens on a Unix domain socket for agent messages
    - Broadcasts them to all connected WebSocket dashboard clients
    - Also accepts commands from dashboard and forwards to agents
    """

    def __init__(
        self,
        socket_path: str = _DEFAULT_SOCKET,
        ws_host: str = "127.0.0.1",
        ws_port: int = _DEFAULT_WS_PORT,
    ) -> None:
        self.socket_path = socket_path
        self.ws_host = ws_host
        self.ws_port = ws_port
        self._ws_clients: set[Any] = set()
        self._agent_writers: dict[str, asyncio.StreamWriter] = {}
        self._message_buffer: list[RelayMessage] = []
        self._max_buffer = 500
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish(self, agent_id: str, payload: dict[str, Any]) -> None:
        """Publish a message from an agent to all dashboard clients."""
        msg = RelayMessage(agent_id=agent_id, payload=payload)
        self._buffer(msg)
        await self._broadcast_ws(msg)

    async def send_to_agent(self, agent_id: str, payload: dict[str, Any]) -> bool:
        """Send a command from the dashboard to a specific agent."""
        writer = self._agent_writers.get(agent_id)
        if not writer:
            logger.warning("Relay: no agent connection for %s", agent_id)
            return False
        try:
            msg = RelayMessage(agent_id=agent_id, payload=payload)
            writer.write((msg.to_json() + "\n").encode())
            await writer.drain()
            return True
        except Exception as exc:
            logger.error("Relay: failed to send to agent %s: %s", agent_id, exc)
            self._agent_writers.pop(agent_id, None)
            return False

    def get_recent_messages(self, n: int = 100) -> list[dict[str, Any]]:
        """Return the last n buffered messages."""
        return [
            {"agent_id": m.agent_id, "payload": m.payload, "timestamp": m.timestamp}
            for m in self._message_buffer[-n:]
        ]

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start Unix socket server + WebSocket server concurrently."""
        self._running = True
        await asyncio.gather(
            self._run_unix_server(),
            self._run_ws_server(),
        )

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Unix domain socket server (agent side)
    # ------------------------------------------------------------------

    async def _run_unix_server(self) -> None:
        """Listen for agent connections over Unix socket."""
        # Remove stale socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        server = await asyncio.start_unix_server(
            self._handle_agent_connection, path=self.socket_path
        )
        Path(self.socket_path).chmod(0o660)
        logger.info("Relay Unix socket listening at %s", self.socket_path)
        async with server:
            await server.serve_forever()

    async def _handle_agent_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one agent connection on the Unix socket."""
        agent_id: str | None = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = RelayMessage.from_json(line.decode().strip())
                    agent_id = msg.agent_id
                    self._agent_writers[agent_id] = writer
                    self._buffer(msg)
                    await self._broadcast_ws(msg)
                except json.JSONDecodeError:
                    logger.debug("Relay: malformed message from agent")
        except Exception as exc:
            logger.debug("Relay: agent connection closed: %s", exc)
        finally:
            if agent_id:
                self._agent_writers.pop(agent_id, None)
            writer.close()

    # ------------------------------------------------------------------
    # WebSocket server (dashboard side)
    # ------------------------------------------------------------------

    async def _run_ws_server(self) -> None:
        """WebSocket server that dashboard connects to."""
        try:
            from aiohttp import web

            app = web.Application()
            app.router.add_get("/ws", self._handle_ws_client)
            app.router.add_get("/relay/history", self._handle_history)
            app.router.add_post("/relay/send", self._handle_send_to_agent)

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, self.ws_host, self.ws_port)
            await site.start()
            logger.info(
                "Relay WebSocket server started at ws://%s:%d/ws",
                self.ws_host, self.ws_port,
            )
            # Keep alive
            while self._running:
                await asyncio.sleep(1)
            await runner.cleanup()
        except ImportError:
            logger.warning("aiohttp not installed — Relay WebSocket server disabled")

    async def _handle_ws_client(self, request: Any) -> Any:
        """Handle a dashboard WebSocket client."""
        from aiohttp import web, WSMsgType

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info("Relay: dashboard client connected (total=%d)", len(self._ws_clients))

        # Send recent history on connect
        for msg in self._message_buffer[-50:]:
            await ws.send_str(msg.to_json())

        try:
            async for ws_msg in ws:
                if ws_msg.type == WSMsgType.TEXT:
                    # Dashboard sending a command to an agent
                    try:
                        data = json.loads(ws_msg.data)
                        agent_id = data.get("agent_id", "")
                        payload = data.get("payload", {})
                        await self.send_to_agent(agent_id, payload)
                    except Exception:
                        pass
                elif ws_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def _handle_history(self, request: Any) -> Any:
        """REST endpoint returning recent message history."""
        from aiohttp import web
        n = int(request.rel_url.query.get("n", 100))
        return web.Response(
            content_type="application/json",
            text=json.dumps({"messages": self.get_recent_messages(n)}),
        )

    async def _handle_send_to_agent(self, request: Any) -> Any:
        """REST endpoint to push a command to an agent."""
        from aiohttp import web
        data = await request.json()
        agent_id = data.get("agent_id", "")
        payload = data.get("payload", {})
        ok = await self.send_to_agent(agent_id, payload)
        return web.Response(
            content_type="application/json",
            text=json.dumps({"success": ok}),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _buffer(self, msg: RelayMessage) -> None:
        self._message_buffer.append(msg)
        if len(self._message_buffer) > self._max_buffer:
            self._message_buffer = self._message_buffer[-self._max_buffer:]

    async def _broadcast_ws(self, msg: RelayMessage) -> None:
        if not self._ws_clients:
            return
        text = msg.to_json()
        dead: set[Any] = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(text)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead


# ---------------------------------------------------------------------------
# Agent-side relay client (lightweight sender)
# ---------------------------------------------------------------------------

class RelayClient:
    """Lightweight client for an agent to publish events to the relay."""

    def __init__(self, socket_path: str = _DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            _, self._writer = await asyncio.open_unix_connection(self.socket_path)
            logger.debug("RelayClient: connected to %s", self.socket_path)
        except Exception as exc:
            logger.warning("RelayClient: could not connect to relay: %s", exc)

    async def publish(self, agent_id: str, payload: dict[str, Any]) -> None:
        if not self._writer:
            await self.connect()
        if not self._writer:
            return
        try:
            msg = RelayMessage(agent_id=agent_id, payload=payload)
            self._writer.write((msg.to_json() + "\n").encode())
            await self._writer.drain()
        except Exception as exc:
            logger.warning("RelayClient: publish failed: %s — reconnecting", exc)
            self._writer = None

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            self._writer = None

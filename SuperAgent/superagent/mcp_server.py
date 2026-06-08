"""Model Context Protocol (MCP) server for SuperAgent.

Exposes SuperAgent desktop-control actions as MCP tools so any compatible
AI host (Claude Code, Codex CLI, etc.) can invoke them transparently.

Protocol reference: https://modelcontextprotocol.io/
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP data models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MCPTool:
    """Descriptor for a single MCP tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MCPRequest:
    """Parsed JSON-RPC 2.0 request from an MCP host."""

    id: Any
    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MCPResponse:
    """JSON-RPC 2.0 response envelope."""

    id: Any
    result: Any = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error:
            envelope["error"] = self.error
        else:
            envelope["result"] = self.result
        return envelope


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Built-in tool descriptors that map to AgentLoop / DesktopAPI operations.
BUILTIN_TOOLS: list[MCPTool] = [
    MCPTool(
        name="screenshot",
        description="Capture the current desktop screenshot and return it as base64 PNG.",
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    MCPTool(
        name="click",
        description="Click the mouse at (x, y) with an optional button.",
        input_schema={
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            },
            "required": ["x", "y"],
        },
    ),
    MCPTool(
        name="type_text",
        description="Type text into the currently focused element.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "enter": {"type": "boolean", "default": False},
            },
            "required": ["text"],
        },
    ),
    MCPTool(
        name="run_command",
        description="Execute a shell command and return stdout/stderr.",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    ),
    MCPTool(
        name="pause_agent",
        description="Pause the SuperAgent execution loop.",
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    MCPTool(
        name="resume_agent",
        description="Resume a paused SuperAgent execution loop.",
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    MCPTool(
        name="inject_instruction",
        description="Inject a human instruction into the running agent loop.",
        input_schema={
            "type": "object",
            "properties": {"instruction": {"type": "string"}},
            "required": ["instruction"],
        },
    ),
    MCPTool(
        name="get_status",
        description="Return the current agent loop status (running, paused, step count).",
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

class MCPServer:
    """Async MCP server that speaks JSON-RPC 2.0 over stdio or TCP.

    Usage (stdio — compatible with Claude Code MCP host):
        server = MCPServer(agent=my_superagent)
        await server.serve_stdio()

    Usage (TCP — for network-accessible tool servers):
        await server.serve_tcp(host="127.0.0.1", port=8765)
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, agent: Any = None, extra_tools: list[MCPTool] | None = None) -> None:
        self.agent = agent
        self.tools: list[MCPTool] = list(BUILTIN_TOOLS) + (extra_tools or [])
        self._tool_map: dict[str, MCPTool] = {t.name: t for t in self.tools}

    # ------------------------------------------------------------------
    # Public serve entrypoints
    # ------------------------------------------------------------------

    async def serve_stdio(self) -> None:
        """Read JSON-RPC requests from stdin and write responses to stdout."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, __import__("sys").stdin.buffer)
        writer_transport, writer_protocol = await loop.connect_write_pipe(
            lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()),
            __import__("sys").stdout.buffer,
        )
        logger.info("MCP server running on stdio")
        while True:
            try:
                line = await reader.readline()
                if not line:
                    break
                response = await self._handle_raw(line)
                if response is not None:
                    writer_transport.write((json.dumps(response) + "\n").encode())
            except Exception as exc:
                logger.exception("MCP stdio error: %s", exc)

    async def serve_tcp(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Accept MCP connections on a TCP socket."""
        server = await asyncio.start_server(self._handle_connection, host, port)
        logger.info("MCP server listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.debug("MCP connection from %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response = await self._handle_raw(line)
                if response is not None:
                    writer.write((json.dumps(response) + "\n").encode())
                    await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    # ------------------------------------------------------------------
    # Protocol dispatch
    # ------------------------------------------------------------------

    async def _handle_raw(self, raw: bytes) -> dict[str, Any] | None:
        try:
            data = json.loads(raw.decode())
        except json.JSONDecodeError as exc:
            return MCPResponse(id=None, error={"code": -32700, "message": str(exc)}).to_dict()

        req = MCPRequest(
            id=data.get("id"),
            method=data.get("method", ""),
            params=data.get("params", {}),
        )
        return await self._dispatch(req)

    async def _dispatch(self, req: MCPRequest) -> dict[str, Any]:
        method = req.method
        try:
            if method == "initialize":
                result = self._handle_initialize(req.params)
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tool_call(req.params)
            elif method == "ping":
                result = {}
            else:
                return MCPResponse(
                    id=req.id,
                    error={"code": -32601, "message": f"Method not found: {method}"},
                ).to_dict()
        except Exception as exc:
            logger.exception("MCP dispatch error for %s", method)
            return MCPResponse(
                id=req.id,
                error={"code": -32603, "message": str(exc)},
            ).to_dict()
        return MCPResponse(id=req.id, result=result).to_dict()

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "SuperAgent MCP", "version": "1.0.0"},
        }

    def _handle_tools_list(self) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in self.tools
            ]
        }

    async def _handle_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in self._tool_map:
            raise ValueError(f"Unknown tool: {tool_name}")

        result = await self._invoke_tool(tool_name, arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        }

    async def _invoke_tool(self, name: str, args: dict[str, Any]) -> Any:
        """Route a tool call to the connected agent or desktop API."""
        desktop = getattr(self.agent, "desktop_api", None)
        loop = getattr(self.agent, "loop", None)

        if name == "screenshot":
            if desktop:
                data = await desktop.screenshot()
                import base64
                return {"screenshot_b64": base64.b64encode(data).decode(), "format": "png"}
            return {"error": "no desktop_api connected"}

        elif name == "click":
            if desktop:
                await desktop.click(args["x"], args["y"], button=args.get("button", "left"))
                return {"ok": True}
            return {"error": "no desktop_api connected"}

        elif name == "type_text":
            if desktop:
                await desktop.type_text(args["text"])
                if args.get("enter"):
                    await desktop.press_keys(["Return"])
                return {"ok": True}
            return {"error": "no desktop_api connected"}

        elif name == "run_command":
            if desktop:
                output = await desktop.run_command(args["command"])
                return {"output": output}
            return {"error": "no desktop_api connected"}

        elif name == "pause_agent":
            if loop:
                loop.pause()
                return {"paused": True}
            return {"error": "no loop connected"}

        elif name == "resume_agent":
            if loop:
                loop.resume()
                return {"paused": False}
            return {"error": "no loop connected"}

        elif name == "inject_instruction":
            if loop:
                loop.inject_instruction(args["instruction"])
                return {"ok": True}
            return {"error": "no loop connected"}

        elif name == "get_status":
            if loop:
                state = loop.state
                return {
                    "paused": state.paused,
                    "done": state.done,
                    "step_count": state.step_count,
                    "objective": state.objective,
                }
            return {"status": "no loop connected"}

        raise ValueError(f"Unhandled tool: {name}")

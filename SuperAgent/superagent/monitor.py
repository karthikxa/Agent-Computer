"""Agent watchdogs and liveness checks."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


RestartCallback = Callable[[str], Awaitable[None] | None]


@dataclass
class AgentHeartbeat:
    """State tracked for a registered agent."""

    agent_id: str
    last_seen: float
    metadata: dict[str, Any] = field(default_factory=dict)
    restart_count: int = 0


class WatchdogManager:
    """Background watchdog that pings and restarts unhealthy agents."""

    def __init__(self, *, heartbeat_interval_seconds: int = 30) -> None:
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._agents: dict[str, AgentHeartbeat] = {}
        self._restart_callbacks: dict[str, RestartCallback] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def register(self, agent_id: str, *, restart_callback: RestartCallback | None = None, metadata: dict[str, Any] | None = None) -> None:
        """Register an agent with the watchdog."""

        self._agents[agent_id] = AgentHeartbeat(agent_id=agent_id, last_seen=asyncio.get_running_loop().time(), metadata=metadata or {})
        if restart_callback is not None:
            self._restart_callbacks[agent_id] = restart_callback

    def heartbeat(self, agent_id: str, metadata: dict[str, Any] | None = None) -> None:
        """Record a heartbeat for an agent."""

        agent = self._agents.setdefault(agent_id, AgentHeartbeat(agent_id=agent_id, last_seen=asyncio.get_running_loop().time()))
        agent.last_seen = asyncio.get_running_loop().time()
        if metadata:
            agent.metadata.update(metadata)

    async def start(self) -> None:
        """Start the watchdog loop."""

        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the watchdog loop."""

        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        """Heartbeat loop that checks every interval and restarts dead agents."""

        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            now = loop.time()
            for agent_id, heartbeat in list(self._agents.items()):
                if now - heartbeat.last_seen <= self.heartbeat_interval_seconds:
                    continue
                callback = self._restart_callbacks.get(agent_id)
                if callback is None:
                    heartbeat.last_seen = now
                    continue
                heartbeat.restart_count += 1
                result = callback(agent_id)
                if asyncio.iscoroutine(result):
                    await result
                heartbeat.last_seen = loop.time()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.heartbeat_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def watch(self) -> None:
        """Public wrapper around the background run loop."""

        await self._run()

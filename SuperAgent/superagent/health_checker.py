"""Per-agent health checker with liveness and readiness probes.

Feature #87 — inspired by Kubernetes health probes.

Each agent container exposes an HTTP /health endpoint.  The HealthChecker
polls all registered agents and:
  - Marks agents as LIVE / STALLED / DEAD
  - Triggers restart via ContainerManager if an agent is DEAD
  - Reports health history to the dashboard

Usage::

    hc = HealthChecker(container_manager=mgr)
    hc.register("agent-1", health_url="http://127.0.0.1:8001/health")
    asyncio.create_task(hc.run_forever(interval=10.0))

    status = hc.get_status("agent-1")   # "live" | "stalled" | "dead"
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0
_STALL_AFTER_SECS = 30.0
_DEAD_AFTER_SECS  = 90.0


class AgentHealth(str, Enum):
    LIVE    = "live"
    STALLED = "stalled"
    DEAD    = "dead"
    UNKNOWN = "unknown"


@dataclass
class HealthRecord:
    """Health probe result for a single agent."""
    agent_id: str
    health_url: str
    status: AgentHealth = AgentHealth.UNKNOWN
    last_seen: float = 0.0
    consecutive_failures: int = 0
    last_check: float = field(default_factory=time.time)
    response_ms: float = 0.0
    auto_restart: bool = True


class HealthChecker:
    """Poll agent /health endpoints and manage liveness state."""

    def __init__(
        self,
        container_manager: Any | None = None,
        *,
        stall_threshold_secs: float = _STALL_AFTER_SECS,
        dead_threshold_secs: float = _DEAD_AFTER_SECS,
        probe_timeout_secs: float = _DEFAULT_TIMEOUT,
        max_restarts: int = 3,
    ) -> None:
        self.container_manager = container_manager
        self.stall_threshold = stall_threshold_secs
        self.dead_threshold = dead_threshold_secs
        self.probe_timeout = probe_timeout_secs
        self.max_restarts = max_restarts
        self._agents: dict[str, HealthRecord] = {}
        self._restart_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        agent_id: str,
        *,
        health_url: str,
        auto_restart: bool = True,
    ) -> None:
        """Register an agent for health checking."""
        self._agents[agent_id] = HealthRecord(
            agent_id=agent_id,
            health_url=health_url,
            last_seen=time.time(),
            auto_restart=auto_restart,
        )
        logger.debug("HealthChecker: registered %s at %s", agent_id, health_url)

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Monitoring loop
    # ------------------------------------------------------------------

    async def run_forever(self, interval: float = 10.0) -> None:
        """Feature #87 — Continuously probe all registered agents."""
        logger.info("HealthChecker: started (interval=%.0fs)", interval)
        while True:
            await asyncio.gather(
                *(self._probe(rec) for rec in list(self._agents.values())),
                return_exceptions=True,
            )
            await asyncio.sleep(interval)

    async def probe_once(self) -> dict[str, str]:
        """Run a single health check on all agents. Returns {agent_id: status}."""
        await asyncio.gather(
            *(self._probe(rec) for rec in list(self._agents.values())),
            return_exceptions=True,
        )
        return {aid: rec.status.value for aid, rec in self._agents.items()}

    # ------------------------------------------------------------------
    # Status API
    # ------------------------------------------------------------------

    def get_status(self, agent_id: str) -> str:
        rec = self._agents.get(agent_id)
        return rec.status.value if rec else AgentHealth.UNKNOWN.value

    def get_all_statuses(self) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": rec.agent_id,
                "status": rec.status.value,
                "last_seen": rec.last_seen,
                "consecutive_failures": rec.consecutive_failures,
                "response_ms": rec.response_ms,
                "last_check": rec.last_check,
            }
            for rec in self._agents.values()
        ]

    def live_agents(self) -> list[str]:
        return [aid for aid, rec in self._agents.items() if rec.status == AgentHealth.LIVE]

    def dead_agents(self) -> list[str]:
        return [aid for aid, rec in self._agents.items() if rec.status == AgentHealth.DEAD]

    # ------------------------------------------------------------------
    # Internal probe logic
    # ------------------------------------------------------------------

    async def _probe(self, rec: HealthRecord) -> None:
        import aiohttp
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    rec.health_url,
                    timeout=aiohttp.ClientTimeout(total=self.probe_timeout),
                ) as resp:
                    ok = resp.status == 200
        except Exception:
            ok = False

        rec.last_check = time.time()
        rec.response_ms = (time.monotonic() - t0) * 1000

        if ok:
            rec.last_seen = rec.last_check
            rec.consecutive_failures = 0
            rec.status = AgentHealth.LIVE
        else:
            rec.consecutive_failures += 1
            elapsed = rec.last_check - rec.last_seen if rec.last_seen else 0
            if elapsed > self.dead_threshold:
                prev_status = rec.status
                rec.status = AgentHealth.DEAD
                if prev_status != AgentHealth.DEAD:
                    await self._handle_dead(rec)
            elif elapsed > self.stall_threshold:
                rec.status = AgentHealth.STALLED
                logger.warning("HealthChecker: agent %s STALLED (%.0fs)", rec.agent_id, elapsed)

    async def _handle_dead(self, rec: HealthRecord) -> None:
        logger.error("HealthChecker: agent %s DEAD", rec.agent_id)
        if not rec.auto_restart or not self.container_manager:
            return
        restarts = self._restart_counts.get(rec.agent_id, 0)
        if restarts >= self.max_restarts:
            logger.error(
                "HealthChecker: agent %s exceeded max restarts (%d), giving up",
                rec.agent_id, self.max_restarts,
            )
            return
        try:
            agent_int_id = int(rec.agent_id.split("-")[-1])
            await self.container_manager.restart(agent_int_id)
            self._restart_counts[rec.agent_id] = restarts + 1
            rec.last_seen = time.time()
            rec.status = AgentHealth.UNKNOWN
            logger.info(
                "HealthChecker: restarted agent %s (attempt %d/%d)",
                rec.agent_id, restarts + 1, self.max_restarts,
            )
        except Exception as exc:
            logger.error("HealthChecker: restart failed for %s: %s", rec.agent_id, exc)

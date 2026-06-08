"""Agent pool and task distribution."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from .agent import SuperAgent
from .config import AgentConfig
from .queue import PriorityTaskQueue


@dataclass(slots=True)
class PoolStatus:
    """Status snapshot for an agent."""

    agent_id: str
    busy: bool
    tasks_completed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentPool:
    """Coordinate several SuperAgent instances."""

    def __init__(self) -> None:
        self._agents: dict[str, SuperAgent] = {}
        self._status: dict[str, PoolStatus] = {}
        self._task_counter = itertools.count(1)
        self.queue = PriorityTaskQueue()

    def create_agent(self, config: AgentConfig) -> SuperAgent:
        """Create and register a new agent."""

        agent = SuperAgent(config)
        self._agents[config.agent_id] = agent
        self._status[config.agent_id] = PoolStatus(agent_id=config.agent_id, busy=False)
        return agent

    async def assign_task(self, agent_id: str, objective: str, *, priority: int = 100) -> list[Any]:
        """Assign a task to a specific agent."""

        agent = self._agents[agent_id]
        await self.queue.enqueue(f"task-{next(self._task_counter)}", {"agent_id": agent_id, "objective": objective}, priority=priority)
        self._status[agent_id].busy = True
        result = await agent.run(objective)
        self._status[agent_id].busy = False
        self._status[agent_id].tasks_completed += 1
        return result

    def get_status(self, agent_id: str | None = None) -> dict[str, PoolStatus] | PoolStatus | None:
        """Return one agent status or all statuses."""

        if agent_id is None:
            return dict(self._status)
        return self._status.get(agent_id)

    async def shutdown(self) -> None:
        """Stop all agents."""

        await asyncio.gather(*(agent.stop() for agent in self._agents.values()), return_exceptions=True)


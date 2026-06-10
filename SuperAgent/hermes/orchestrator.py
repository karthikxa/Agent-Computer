"""Hermes task orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from infrastructure.container_manager import ContainerManager
from infrastructure.logging import configure_logging
from infrastructure.task_db import TaskDatabase


@dataclass(slots=True)
class HermesTask:
    """A decomposed Hermes task."""

    instruction: str
    expected_output: str
    priority: int = 100


class HermesOrchestrator:
    """Coordinate work across many agent containers."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_agents: int = 250,
        container_manager: ContainerManager | None = None,
        task_db: TaskDatabase | None = None,
        log_dir: str | None = None,
    ) -> None:
        self.model = model or os.getenv("HERMES_MODEL", "NousResearch/Hermes-3-Llama-3.1-70B")
        self.api_key = api_key or os.getenv("TOGETHER_API_KEY") or os.getenv("OLLAMA_API_KEY", "")
        self.base_url = base_url or os.getenv("HERMES_BASE_URL", "https://api.together.xyz/v1")
        self.max_agents = max_agents
        self.container_manager = container_manager or ContainerManager(max_agents=max_agents)
        self.task_db = task_db or TaskDatabase(Path(os.getenv("DB_PATH", "./data/superagent.db")))  # type: ignore[name-defined]
        self.logger = configure_logging(log_dir or os.getenv("LOG_PATH", "./logs"))

    async def run(self, command: str) -> str:
        """Entry point that decomposes, runs, and aggregates work."""

        n_agents = await self._choose_agent_count(command)
        subtasks = await self.decompose(command, n_agents)
        await self.container_manager.spawn_all(min(n_agents, self.max_agents))
        task_ids: list[int] = []
        for index, subtask in enumerate(subtasks, start=1):
            task_id = await self.task_db.create_task(command, subtask["instruction"], subtask.get("priority", 100))
            task_ids.append(task_id)
            await self.task_db.assign_task(task_id, str(index))

        monitor_task = asyncio.create_task(self.monitor(command))
        try:
            await asyncio.wait_for(self._wait_for_completion(task_ids), timeout=3600)
        except asyncio.TimeoutError:
            self.logger.error(
                "Hermes task timed out",
                extra={"agent_id": "hermes", "task_id": command},
            )
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

        results = [f"task {task_id} completed" for task_id in task_ids]
        return await self.aggregate(command, results)

    async def _chat(self, prompt: str) -> str:
        """Call Hermes or fall back to local heuristics."""

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url.rstrip('/')}/chat/completions", json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
        except Exception:
            return ""

    async def _choose_agent_count(self, command: str) -> int:
        """Choose a reasonable worker count from task complexity."""

        prompt = (
            "Estimate a worker count from 1 to 250 for this task. "
            "Return only an integer.\nTask: "
            + command
        )
        response = await self._chat(prompt)
        try:
            count = int("".join(ch for ch in response if ch.isdigit())[:3] or "1")
        except ValueError:
            count = 1
        return max(1, min(250, count or 1))

    async def decompose(self, command: str, n_agents: int) -> list[dict[str, Any]]:
        """Split a goal into parallel subtasks."""

        prompt = (
            f"Decompose this goal into {n_agents} parallel subtasks. "
            "Return JSON list of objects with instruction, expected_output, priority.\n"
            f"Goal: {command}"
        )
        response = await self._chat(prompt)
        try:
            parsed = json.loads(response)
            if isinstance(parsed, list):
                return parsed[:n_agents]
        except json.JSONDecodeError:
            pass
        return [
            {
                "instruction": f"Work on part {idx + 1} of: {command}",
                "expected_output": command,
                "priority": idx + 1,
            }
            for idx in range(n_agents)
        ]

    async def aggregate(self, command: str, results: list[str]) -> str:
        """Synthesize a final answer from worker results."""

        prompt = (
            "Synthesize one final answer from the agent results.\n"
            f"Goal: {command}\nResults:\n" + "\n".join(results)
        )
        response = await self._chat(prompt)
        return response.strip() or "\n".join(results)

    async def monitor(self, task_id: str) -> None:
        """Poll agents every 5 seconds and reassign stalled work."""

        while True:
            dead_agents = await self.task_db.get_dead_agents(timeout=30)
            if dead_agents:
                idle = [agent["id"] for agent in (await self.task_db.get_workforce_status())["agents"] if agent.get("status") == "idle"]
                failed_tasks = []
                for agent in dead_agents:
                    if agent.get("current_task_id"):
                        failed_tasks.append(int(agent["current_task_id"]))
                        await self.container_manager.restart(int(agent["id"]))
                if failed_tasks and idle:
                    await self.rebalance(failed_tasks, idle)
            await asyncio.sleep(5)

    async def rebalance(self, failed_tasks: list[int], idle_agents: list[str]) -> None:
        """Redistribute failed tasks."""

        for task_id, agent_id in zip(failed_tasks, idle_agents, strict=False):
            await self.task_db.assign_task(task_id, agent_id)

    async def _wait_for_completion(self, task_ids: list[int]) -> None:
        """Wait until all tasks are finished."""

        while True:
            pending = await self.task_db.get_all_pending()
            workforce = await self.task_db.get_workforce_status()
            running = workforce["tasks"]["running"]
            if not pending and running == 0:
                return
            await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Feature #72 — Agent grouping: logical teams of agents
    # ------------------------------------------------------------------

    async def create_group(
        self,
        group_name: str,
        agent_ids: list[int],
        *,
        shared_goal: str = "",
        shared_memory: bool = False,
    ) -> dict[str, Any]:
        """Create a logical group of agents that share a goal.

        Groups are persisted to the task DB and can be monitored as a
        unit via get_group_status().

        Parameters
        ----------
        group_name:
            Human-readable group identifier (e.g. 'research-team').
        agent_ids:
            List of integer agent IDs to include in this group.
        shared_goal:
            Overarching goal for the group (stored as metadata).
        shared_memory:
            If True, agents in the group share a common memory namespace.
        """
        group_id = f"group-{group_name}-{int(asyncio.get_event_loop().time())}"
        if not hasattr(self, "_groups"):
            self._groups: dict[str, Any] = {}
        self._groups[group_id] = {
            "group_id": group_id,
            "group_name": group_name,
            "agent_ids": agent_ids,
            "shared_goal": shared_goal,
            "shared_memory": shared_memory,
            "created_at": asyncio.get_event_loop().time(),
            "status": "active",
        }
        self.logger.info(
            "Hermes: created group '%s' with %d agents", group_name, len(agent_ids)
        )
        return self._groups[group_id]

    async def dissolve_group(self, group_id: str) -> bool:
        """Dissolve a group (agents continue running independently)."""
        if not hasattr(self, "_groups"):
            return False
        group = self._groups.pop(group_id, None)
        if group:
            self.logger.info("Hermes: dissolved group %s", group_id)
        return group is not None

    async def get_group_status(self, group_id: str) -> dict[str, Any]:
        """Return current status of all agents in a group."""
        if not hasattr(self, "_groups"):
            return {"error": "No groups defined"}
        group = self._groups.get(group_id)
        if not group:
            return {"error": f"Group '{group_id}' not found"}

        workforce = await self.task_db.get_workforce_status()
        agent_statuses = []
        for aid in group["agent_ids"]:
            # Look up per-agent status from workforce report
            all_agents = workforce.get("agents", [])
            match = next((a for a in all_agents if str(a.get("id")) == str(aid)), None)
            agent_statuses.append(match or {"id": aid, "status": "unknown"})

        return {**group, "agents": agent_statuses}

    async def list_groups(self) -> list[dict[str, Any]]:
        """List all active groups."""
        return list(getattr(self, "_groups", {}).values())

    async def broadcast_to_group(self, group_id: str, message: str) -> int:
        """Send a message/instruction to all agents in a group.

        Returns the number of agents that received the message.
        """
        if not hasattr(self, "_groups"):
            return 0
        group = self._groups.get(group_id)
        if not group:
            return 0
        sent = 0
        for aid in group["agent_ids"]:
            try:
                task_id = await self.task_db.create_task(
                    message, f"Group instruction for agent {aid}", priority=50
                )
                await self.task_db.assign_task(task_id, str(aid))
                sent += 1
            except Exception:
                pass
        return sent

    # ------------------------------------------------------------------
    # Feature #88 — Priority task queue
    # ------------------------------------------------------------------

    async def submit_priority(
        self,
        command: str,
        *,
        priority: int = 100,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Submit a task with explicit priority and optional deadline.

        Priority scale: 1 (highest) → 1000 (lowest).
        Tasks with priority < 50 are treated as URGENT and skip the
        normal queue to be assigned immediately to idle agents.
        """
        subtasks = await self.decompose(command, 1)
        subtask = subtasks[0] if subtasks else {"instruction": command, "priority": priority}
        subtask["priority"] = priority

        task_id = await self.task_db.create_task(
            command,
            subtask["instruction"],
            priority,
        )

        # URGENT: assign immediately to any idle agent
        if priority < 50:
            workforce = await self.task_db.get_workforce_status()
            idle_agents = [
                a["id"] for a in workforce.get("agents", [])
                if a.get("status") == "idle"
            ]
            if idle_agents:
                await self.task_db.assign_task(task_id, str(idle_agents[0]))
                self.logger.info(
                    "Hermes: URGENT task %d assigned immediately to agent %s",
                    task_id, idle_agents[0],
                )

        return {
            "task_id": task_id,
            "priority": priority,
            "deadline_seconds": deadline_seconds,
            "status": "queued",
        }

    # ------------------------------------------------------------------
    # Feature #93 — Auto-scaling: spawn/terminate agents based on load
    # ------------------------------------------------------------------

    async def auto_scale(
        self,
        *,
        min_agents: int = 1,
        max_agents: int | None = None,
        target_queue_depth: int = 5,
    ) -> dict[str, Any]:
        """Dynamically scale the agent pool based on pending task queue depth.

        Spawns new agents if queue depth > target and terminates idle agents
        when queue depth is low.

        Parameters
        ----------
        min_agents:
            Minimum agents to keep running at all times.
        max_agents:
            Maximum agents (defaults to self.max_agents).
        target_queue_depth:
            Desired tasks-per-agent ratio.
        """
        max_agents = max_agents or self.max_agents
        workforce = await self.task_db.get_workforce_status()
        pending = len(await self.task_db.get_all_pending())
        current_count = len(self.container_manager.list_running())
        desired = max(min_agents, min(max_agents, (pending // max(1, target_queue_depth)) + 1))

        spawned = 0
        terminated = 0

        if desired > current_count:
            # Scale up
            to_spawn = desired - current_count
            next_id = current_count + 1
            await self.container_manager.spawn_all(to_spawn, start_id=next_id)
            spawned = to_spawn
            self.logger.info("AutoScale: spawned %d new agents (pending=%d)", to_spawn, pending)

        elif desired < current_count and pending == 0:
            # Scale down — terminate idle agents down to min_agents
            running = self.container_manager.list_running()
            to_stop = current_count - max(min_agents, desired)
            for entry in running[-to_stop:]:
                try:
                    await self.container_manager.stop(int(entry["agent_id"]))
                    terminated += 1
                except Exception:
                    pass
            if terminated:
                self.logger.info("AutoScale: terminated %d idle agents", terminated)

        return {
            "previous_count": current_count,
            "desired_count": desired,
            "spawned": spawned,
            "terminated": terminated,
            "pending_tasks": pending,
        }


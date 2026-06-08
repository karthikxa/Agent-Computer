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

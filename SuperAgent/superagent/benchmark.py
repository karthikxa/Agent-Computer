"""OSWorld-style benchmark task runner for SuperAgent.

Exposes a BenchmarkRunner that takes task suites (e.g. OSWorld objectives),
executes them sequentially, tracks duration/steps/LLM cost, and yields reports.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import SuperAgent

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkTask:
    """Benchmark task model."""

    task_id: str
    objective: str
    validator: Callable[[Any], bool]  # custom validation function taking agent reference
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    """Benchmark result report."""

    task_id: str
    success: bool
    steps: int
    duration_seconds: float
    total_cost: float
    error: str | None = None


class BenchmarkRunner:
    """Benchmark runner executing benchmark tasks and collecting metrics."""

    def __init__(self, agent: SuperAgent) -> None:
        self.agent = agent

    async def run_task(self, task: BenchmarkTask) -> BenchmarkResult:
        """Run a single benchmark task and return results."""
        logger.info("Starting benchmark task: %s (%s)", task.task_id, task.objective)
        start_time = time.monotonic()
        steps = 0
        success = False
        error_msg = None

        try:
            # Run objective using agent loop
            await self.agent.run(task.objective)
            
            # Check validation
            success = task.validator(self.agent)
            steps = self.agent.loop.state.step_count
        except Exception as e:
            logger.exception("Benchmark task failed due to error: %s", e)
            error_msg = str(e)
        
        duration = time.monotonic() - start_time
        cost = self.agent.runtime.cost_tracker.get_total_cost()

        return BenchmarkResult(
            task_id=task.task_id,
            success=success,
            steps=steps,
            duration_seconds=duration,
            total_cost=cost,
            error=error_msg,
        )

    async def run_suite(self, tasks: list[BenchmarkTask]) -> list[BenchmarkResult]:
        """Run all tasks in a benchmark suite."""
        results = []
        for task in tasks:
            res = await self.run_task(task)
            results.append(res)
        return results

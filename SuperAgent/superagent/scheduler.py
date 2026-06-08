"""Async scheduling helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

try:  # pragma: no cover - optional dependency
    from apscheduler.schedulers.asyncio import AsyncIOScheduler as _APAsyncIOScheduler  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    _APAsyncIOScheduler = None  # type: ignore[assignment]


JobCallable = Callable[..., Awaitable[Any] | Any]


class AsyncIOScheduler:  # pragma: no cover - fallback shim
    """Fallback scheduler used when APScheduler is not installed."""

    def __init__(self) -> None:
        self._jobs: list[asyncio.Task[Any]] = []
        self._running = False

    def add_job(self, func: JobCallable, trigger: str, **kwargs: Any) -> str:
        job_id = kwargs.get("id", f"job-{len(self._jobs) + 1}")
        if trigger == "date":
            run_date = kwargs.get("run_date")
            delay = max(0.0, (run_date - datetime.now(timezone.utc)).total_seconds()) if isinstance(run_date, datetime) else 0.0
            self._jobs.append(asyncio.create_task(_run_once(delay, func, kwargs)))
        elif trigger == "interval":
            seconds = float(kwargs.get("seconds", 0))
            self._jobs.append(asyncio.create_task(_run_interval(seconds, func, kwargs)))
        else:
            raise ValueError(f"Unsupported trigger: {trigger}")
        return job_id

    def start(self) -> None:
        self._running = True

    def shutdown(self, wait: bool = True) -> None:
        self._running = False
        for task in self._jobs:
            task.cancel()

    def get_jobs(self) -> list[asyncio.Task[Any]]:
        return list(self._jobs)


if _APAsyncIOScheduler is not None:  # pragma: no cover - use real scheduler when available
    AsyncIOScheduler = _APAsyncIOScheduler  # type: ignore[assignment]


async def _run_once(delay: float, func: JobCallable, kwargs: dict[str, Any]) -> None:
    await asyncio.sleep(delay)
    await _invoke(func, kwargs)


async def _run_interval(seconds: float, func: JobCallable, kwargs: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(seconds)
        await _invoke(func, kwargs)


async def _invoke(func: JobCallable, kwargs: dict[str, Any]) -> None:
    call_kwargs = {key: value for key, value in kwargs.items() if key not in {"id", "run_date", "seconds"}}
    result = func(**call_kwargs)
    if asyncio.iscoroutine(result):
        await result


@dataclass
class ScheduledTask:
    """A scheduled callback."""

    task_id: str
    trigger: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskScheduler:
    """High-level wrapper around AsyncIOScheduler."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self._jobs: dict[str, ScheduledTask] = {}
        self._started = False

    def start(self) -> None:
        if not self._started:
            self.scheduler.start()
            self._started = True

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    def schedule(self, when: datetime, callback: JobCallable, **kwargs: Any) -> str:
        """Schedule a one-shot task."""

        self.start()
        job = self.scheduler.add_job(callback, "date", run_date=when, **kwargs)
        job_id = job.id if hasattr(job, "id") else job
        self._jobs[job_id] = ScheduledTask(task_id=job_id, trigger="date", metadata={"run_date": when.isoformat(), **kwargs})
        return job_id

    def schedule_interval(self, seconds: float, callback: JobCallable, **kwargs: Any) -> str:
        """Schedule a recurring task."""

        self.start()
        job = self.scheduler.add_job(callback, "interval", seconds=seconds, **kwargs)
        job_id = job.id if hasattr(job, "id") else job
        self._jobs[job_id] = ScheduledTask(task_id=job_id, trigger="interval", metadata={"seconds": seconds, **kwargs})
        return job_id


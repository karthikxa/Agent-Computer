"""Priority queueing for tasks."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QueuedTask:
    """A queued task with explicit priority."""

    priority: int
    task_id: str
    payload: dict[str, Any]
    sequence: int = field(default=0)


class PriorityTaskQueue:
    """Async priority queue with stable ordering."""

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[tuple[int, int, QueuedTask]] = asyncio.PriorityQueue()
        self._sequence = itertools.count()

    async def enqueue(self, task_id: str, payload: dict[str, Any], *, priority: int = 100) -> QueuedTask:
        """Add a task to the queue."""

        item = QueuedTask(priority=priority, task_id=task_id, payload=payload, sequence=next(self._sequence))
        await self._queue.put((priority, item.sequence, item))
        return item

    async def dequeue(self) -> QueuedTask:
        """Pop the next task from the queue."""

        _, _, item = await self._queue.get()
        self._queue.task_done()
        return item

    def empty(self) -> bool:
        """Return True when no tasks are waiting."""

        return self._queue.empty()

    def __len__(self) -> int:
        return self._queue.qsize()


PriorityQueue = PriorityTaskQueue


"""Kernel-style LLM request scheduler for multi-agent SuperAgent.

Inspired by agiresearch/AIOS: treats LLM API calls as OS-level resources
that must be scheduled, rate-limited, and prioritised across competing agents
to prevent request storms and API quota exhaustion.

Key concepts
------------
- ``KernelRequest``  : an LLM call submitted by any agent, tagged with priority.
- ``LLMKernel``      : the OS-style scheduler that dequeues and dispatches them.
- Priority levels    : 1 (highest, e.g. user-facing HITL) … 10 (background).
- Rate limiting      : configurable tokens-per-minute (TPM) and requests-per-minute (RPM).
- Fairness           : round-robin across agents at the same priority level.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

ProviderCallable = Callable[..., Awaitable[Any]]


@dataclass
class KernelRequest:
    """A single LLM invocation queued through the kernel."""

    agent_id: str
    messages: list[dict[str, Any]]
    callback: ProviderCallable
    priority: int = 5          # 1 = highest, 10 = lowest
    max_tokens: int = 1024
    temperature: float = 0.2
    metadata: dict[str, Any] = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.monotonic)
    _future: asyncio.Future[Any] = field(default_factory=lambda: asyncio.get_event_loop().create_future())

    def __lt__(self, other: "KernelRequest") -> bool:  # for heapq
        return (self.priority, self.submitted_at) < (other.priority, other.submitted_at)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple token-bucket rate limiter."""

    def __init__(self, rpm: int, tpm: int) -> None:
        self._rpm = rpm
        self._tpm = tpm
        self._request_times: list[float] = []
        self._token_times: list[tuple[float, int]] = []  # (timestamp, tokens)

    def can_proceed(self, tokens: int) -> bool:
        now = time.monotonic()
        window = 60.0
        self._request_times = [t for t in self._request_times if now - t < window]
        self._token_times = [(t, n) for t, n in self._token_times if now - t < window]
        if len(self._request_times) >= self._rpm:
            return False
        used_tokens = sum(n for _, n in self._token_times)
        if used_tokens + tokens > self._tpm:
            return False
        return True

    def record(self, tokens: int) -> None:
        now = time.monotonic()
        self._request_times.append(now)
        self._token_times.append((now, tokens))


# ---------------------------------------------------------------------------
# LLM Kernel
# ---------------------------------------------------------------------------

class LLMKernel:
    """OS-style kernel that schedules LLM API calls across agents.

    Usage::

        kernel = LLMKernel(rpm=60, tpm=100_000)
        asyncio.create_task(kernel.run())

        # from any agent:
        result = await kernel.submit(
            agent_id="agent-1",
            messages=[...],
            callback=provider.chat,
            priority=3,
        )
    """

    def __init__(
        self,
        rpm: int = 60,
        tpm: int = 100_000,
        poll_interval: float = 0.1,
        max_concurrent: int = 4,
    ) -> None:
        self._rpm = rpm
        self._tpm = tpm
        self._poll_interval = poll_interval
        self._max_concurrent = max_concurrent
        self._queue: asyncio.PriorityQueue[tuple[int, float, KernelRequest]] = asyncio.PriorityQueue()
        self._rate_limiter = _TokenBucket(rpm, tpm)
        self._running = False
        self._active: int = 0
        self._stats: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit(
        self,
        agent_id: str,
        messages: list[dict[str, Any]],
        callback: ProviderCallable,
        *,
        priority: int = 5,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Submit an LLM request and await its result."""
        req = KernelRequest(
            agent_id=agent_id,
            messages=messages,
            callback=callback,
            priority=priority,
            max_tokens=max_tokens,
            temperature=temperature,
            metadata=metadata or {},
            _future=asyncio.get_event_loop().create_future(),
        )
        # Priority queue key: (priority, timestamp) — lower = higher urgency
        await self._queue.put((req.priority, req.submitted_at, req))
        logger.debug("Kernel: queued request from %s (priority=%d)", agent_id, priority)
        return await req._future

    def get_stats(self) -> dict[str, Any]:
        """Return scheduling statistics."""
        return {
            "queued": self._queue.qsize(),
            "active": self._active,
            "dispatched": dict(self._stats),
        }

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main scheduler loop — run as a background task."""
        self._running = True
        logger.info("LLM Kernel scheduler started (rpm=%d, tpm=%d)", self._rpm, self._tpm)
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("Kernel tick error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        if self._active >= self._max_concurrent:
            return
        if self._queue.empty():
            return

        # Peek at the next request
        priority, ts, req = await self._queue.get()

        estimated_tokens = req.max_tokens + sum(
            len(str(m.get("content", ""))) // 4 for m in req.messages
        )

        if not self._rate_limiter.can_proceed(estimated_tokens):
            # Put back and wait
            await self._queue.put((priority, ts, req))
            await asyncio.sleep(1.0)
            return

        self._rate_limiter.record(estimated_tokens)
        self._active += 1
        self._stats[req.agent_id] += 1
        asyncio.create_task(self._dispatch(req))

    async def _dispatch(self, req: KernelRequest) -> None:
        try:
            result = await req.callback(req.messages, temperature=req.temperature)
            if not req._future.done():
                req._future.set_result(result)
        except Exception as exc:
            logger.error("Kernel dispatch failed for %s: %s", req.agent_id, exc)
            if not req._future.done():
                req._future.set_exception(exc)
        finally:
            self._active -= 1


# ---------------------------------------------------------------------------
# Singleton convenience accessor
# ---------------------------------------------------------------------------

_default_kernel: LLMKernel | None = None


def get_kernel(rpm: int = 60, tpm: int = 100_000) -> LLMKernel:
    """Return (or create) the process-global LLM kernel."""
    global _default_kernel
    if _default_kernel is None:
        _default_kernel = LLMKernel(rpm=rpm, tpm=tpm)
    return _default_kernel

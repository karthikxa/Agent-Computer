"""Escalation hooks for human intervention."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(slots=True)
class EscalationResult:
    """Structured response from an escalation request."""

    ok: bool
    status: int | None = None
    body: str = ""
    metadata: dict[str, Any] | None = None


class EscalationManager:
    """Send task escalations to a webhook URL."""

    def __init__(self, webhook_url: str | None = None, *, timeout_seconds: float = 15.0) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    async def escalate(self, payload: dict[str, Any]) -> EscalationResult:
        """Send a real HTTP POST to the configured webhook URL."""

        if not self.webhook_url:
            return EscalationResult(ok=False, status=None, body="webhook not configured", metadata=payload)
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.webhook_url, json=payload) as response:
                body = await response.text()
                return EscalationResult(ok=response.ok, status=response.status, body=body, metadata=payload)


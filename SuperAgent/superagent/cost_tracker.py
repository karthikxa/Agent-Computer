"""Token cost tracker — real-time per-agent spend accounting.

Feature #80 — token cost tracking and budget enforcement.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o":               {"input": 0.005,    "output": 0.015},
    "gpt-4o-mini":          {"input": 0.00015,  "output": 0.0006},
    "gpt-4-turbo":          {"input": 0.010,    "output": 0.030},
    "gpt-3.5-turbo":        {"input": 0.0005,   "output": 0.0015},
    "claude-3-5-sonnet":    {"input": 0.003,    "output": 0.015},
    "claude-3-haiku":       {"input": 0.00025,  "output": 0.00125},
    "gemini-1.5-flash":     {"input": 0.000075, "output": 0.0003},
    "gemini-1.5-pro":       {"input": 0.00125,  "output": 0.005},
    "llama-3-70b":          {"input": 0.0009,   "output": 0.0009},
    "llama-3-8b":           {"input": 0.0002,   "output": 0.0002},
    "osatlas":              {"input": 0.0,      "output": 0.0},
    "default":              {"input": 0.001,    "output": 0.002},
}


class BudgetExceededError(Exception):
    """Raised when an agent's token budget is exceeded."""
    pass


@dataclass
class AgentCostRecord:
    agent_id: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    daily_cost_usd: float = 0.0
    daily_reset_at: float = field(default_factory=time.time)
    calls: int = 0
    daily_budget_usd: float = 0.0
    total_budget_usd: float = 0.0


@dataclass
class CallRecord:
    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: float = field(default_factory=time.time)


class CostTracker:
    """Track and enforce token cost budgets across all agents."""

    def __init__(
        self,
        pricing: dict[str, dict[str, float]] | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        self.pricing = pricing or _DEFAULT_PRICING
        self.persist_path = Path(persist_path) if persist_path else None
        self._agents: dict[str, AgentCostRecord] = {}
        self._call_log: list[CallRecord] = []
        if self.persist_path and self.persist_path.exists():
            self._load()

    def set_budget(self, agent_id: str, *, daily_usd: float = 0.0, total_usd: float = 0.0) -> None:
        rec = self._get_or_create(agent_id)
        rec.daily_budget_usd = daily_usd
        rec.total_budget_usd = total_usd

    def check_budget(self, agent_id: str) -> None:
        """Raise BudgetExceededError if agent is over budget."""
        rec = self._get_or_create(agent_id)
        self._reset_daily_if_needed(rec)
        if rec.daily_budget_usd > 0 and rec.daily_cost_usd >= rec.daily_budget_usd:
            raise BudgetExceededError(
                f"Agent '{agent_id}' daily budget exceeded: "
                f"${rec.daily_cost_usd:.4f} >= ${rec.daily_budget_usd:.2f}/day"
            )
        if rec.total_budget_usd > 0 and rec.total_cost_usd >= rec.total_budget_usd:
            raise BudgetExceededError(
                f"Agent '{agent_id}' total budget exceeded: "
                f"${rec.total_cost_usd:.4f} >= ${rec.total_budget_usd:.2f} total"
            )

    def record(self, agent_id: str, *, model: str, input_tokens: int, output_tokens: int) -> float:
        """Record a completed LLM call and return cost in USD."""
        cost = self._compute_cost(model, input_tokens, output_tokens)
        rec = self._get_or_create(agent_id)
        self._reset_daily_if_needed(rec)
        rec.total_input_tokens  += input_tokens
        rec.total_output_tokens += output_tokens
        rec.total_cost_usd      += cost
        rec.daily_cost_usd      += cost
        rec.calls               += 1
        self._call_log.append(CallRecord(
            agent_id=agent_id, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
        ))
        if self.persist_path:
            self._save()
        return cost

    def get_summary(self, agent_id: str) -> dict[str, Any]:
        rec = self._get_or_create(agent_id)
        self._reset_daily_if_needed(rec)
        return {
            "agent_id": agent_id,
            "calls": rec.calls,
            "total_input_tokens": rec.total_input_tokens,
            "total_output_tokens": rec.total_output_tokens,
            "total_tokens": rec.total_input_tokens + rec.total_output_tokens,
            "total_cost_usd": round(rec.total_cost_usd, 6),
            "daily_cost_usd": round(rec.daily_cost_usd, 6),
            "daily_budget_usd": rec.daily_budget_usd,
            "total_budget_usd": rec.total_budget_usd,
            "daily_remaining_usd": max(0, rec.daily_budget_usd - rec.daily_cost_usd)
                                   if rec.daily_budget_usd else None,
        }

    def get_all_summaries(self) -> list[dict[str, Any]]:
        return sorted(
            [self.get_summary(aid) for aid in self._agents],
            key=lambda x: x["total_cost_usd"],
            reverse=True,
        )

    def global_total(self) -> float:
        return sum(rec.total_cost_usd for rec in self._agents.values())

    def get_call_log(self, agent_id: str | None = None, n: int = 50) -> list[dict[str, Any]]:
        logs = self._call_log if not agent_id else [c for c in self._call_log if c.agent_id == agent_id]
        return [
            {"agent_id": c.agent_id, "model": c.model, "input_tokens": c.input_tokens,
             "output_tokens": c.output_tokens, "cost_usd": round(c.cost_usd, 6), "timestamp": c.timestamp}
            for c in logs[-n:]
        ]

    def _save(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                aid: {
                    "total_input_tokens": r.total_input_tokens,
                    "total_output_tokens": r.total_output_tokens,
                    "total_cost_usd": r.total_cost_usd,
                    "daily_cost_usd": r.daily_cost_usd,
                    "daily_reset_at": r.daily_reset_at,
                    "calls": r.calls,
                    "daily_budget_usd": r.daily_budget_usd,
                    "total_budget_usd": r.total_budget_usd,
                }
                for aid, r in self._agents.items()
            }
            self.persist_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("CostTracker: failed to persist: %s", exc)

    def _load(self) -> None:
        try:
            data = json.loads(self.persist_path.read_text())
            for aid, d in data.items():
                self._agents[aid] = AgentCostRecord(agent_id=aid, **d)
        except Exception as exc:
            logger.warning("CostTracker: failed to load: %s", exc)

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = None
        for key, prices in self.pricing.items():
            if key in model.lower():
                pricing = prices
                break
        if pricing is None:
            pricing = self.pricing.get("default", {"input": 0.001, "output": 0.002})
        return (input_tokens / 1000.0) * pricing["input"] + (output_tokens / 1000.0) * pricing["output"]

    def _get_or_create(self, agent_id: str) -> AgentCostRecord:
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentCostRecord(agent_id=agent_id)
        return self._agents[agent_id]

    @staticmethod
    def _reset_daily_if_needed(rec: AgentCostRecord) -> None:
        if time.time() - rec.daily_reset_at >= 86400:
            rec.daily_cost_usd = 0.0
            rec.daily_reset_at = time.time()

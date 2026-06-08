"""Token and cost accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (5.00, 15.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    # Anthropic
    "claude-opus-4-5": (15.00, 75.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    # Groq
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "mixtral-8x7b-32768": (0.24, 0.24),
    "gemma2-9b-it": (0.20, 0.20),
    # DeepSeek
    "deepseek-chat": (0.14, 0.28),
    "deepseek-coder": (0.14, 0.28),
    # Gemini
    "gemini-1.5-pro": (3.50, 10.50),
    "gemini-1.5-flash": (0.35, 1.05),
    "gemini-2.0-flash": (0.10, 0.40),
    # Mistral
    "mistral-large-latest": (8.00, 24.00),
    "mistral-small-latest": (2.00, 6.00),
    # Fireworks
    "accounts/fireworks/models/llama-v3p1-70b-instruct": (0.90, 0.90),
    # OpenRouter
    "openrouter/auto": (0.00, 0.00),
    # Local
    "local": (0.00, 0.00),
}


@dataclass(slots=True)
class CostEntry:
    """A single usage event."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CostTracker:
    """Track token usage and compute estimated spend."""

    price_table: dict[str, dict[str, float]] = field(default_factory=lambda: DEFAULT_PRICE_TABLE.copy())
    entries: list[CostEntry] = field(default_factory=list)

    def record(self, provider: str, model: str, *, input_tokens: int = 0, output_tokens: int = 0, metadata: dict[str, Any] | None = None) -> CostEntry:
        """Append a usage record."""

        entry = CostEntry(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            metadata=metadata or {},
        )
        self.entries.append(entry)
        return entry

    def estimate(self, provider: str, *, input_tokens: int = 0, output_tokens: int = 0) -> float:
        """Estimate cost using the configured price table."""

        pricing = self.price_table.get(provider.lower(), (0.0, 0.0))
        return input_tokens * pricing[0] / 1_000_000 + output_tokens * pricing[1] / 1_000_000

    def total_cost(self) -> float:
        """Return the running total cost."""

        total = 0.0
        for entry in self.entries:
            total += self.estimate(
                entry.model if entry.model in self.price_table else entry.provider,
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
            )
        return total

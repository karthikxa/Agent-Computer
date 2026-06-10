"""Agent context manager — system-prompt injection and context compression.

Features #58/#59/#61:
  #58 — System-prompt injection per agent (goal, persona, tool list)
  #59 — Context window compression (summarise history when near limit)
  #61 — Cross-agent context sharing via shared memory namespace

Usage::

    ctx = ContextManager(agent_id="agent-1", model="gpt-4o")
    ctx.set_system_prompt("You are a senior researcher. Goal: {goal}", goal="find papers")
    ctx.add_user("Search for quantum computing papers from 2024")
    ctx.add_assistant("I found 42 papers...")

    # Compress when approaching token limit
    await ctx.maybe_compress(token_limit=8000)

    messages = ctx.get_messages()   # ready to send to LLM
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Shared namespace for cross-agent context (#61)
_SHARED_NAMESPACES: dict[str, list[dict[str, str]]] = {}


@dataclass
class ContextManager:
    """Manages the message history and system prompt for one agent.

    Parameters
    ----------
    agent_id:
        Owning agent identifier.
    model:
        LLM model name (used for token estimation).
    max_tokens:
        Soft limit — triggers compression when exceeded.
    compression_target_ratio:
        After compression, keep this fraction of the token budget.
    """

    agent_id: str
    model: str = "gpt-4o"
    max_tokens: int = 8000
    compression_target_ratio: float = 0.6

    _system_prompt: str = field(default="", init=False)
    _messages: list[dict[str, str]] = field(default_factory=list, init=False)
    _token_estimate: int = field(default=0, init=False)
    _compress_fn: Any = field(default=None, init=False)   # injected LLM callable

    # ------------------------------------------------------------------
    # Feature #58 — System prompt injection
    # ------------------------------------------------------------------

    def set_system_prompt(self, template: str, **kwargs: str) -> None:
        """Set the system prompt, optionally formatting with keyword args.

        Template variables like ``{goal}`` and ``{agent_id}`` are filled in.
        """
        kwargs.setdefault("agent_id", self.agent_id)
        try:
            self._system_prompt = template.format(**kwargs)
        except KeyError as exc:
            logger.warning("ContextManager: missing template key %s — using raw template", exc)
            self._system_prompt = template
        logger.debug("ContextManager[%s]: system prompt set (%d chars)", self.agent_id, len(self._system_prompt))

    def get_system_prompt(self) -> str:
        return self._system_prompt

    def inject_tool_list(self, tools: list[dict[str, Any]]) -> None:
        """Append available tool descriptions to the system prompt."""
        if not tools:
            return
        tool_text = "\n\nAvailable tools:\n" + "\n".join(
            f"  - {t.get('name', '?')}: {t.get('description', '')}"
            for t in tools
        )
        self._system_prompt = (self._system_prompt or "") + tool_text

    def inject_persona(self, persona: str) -> None:
        """Prepend a persona description to the system prompt."""
        self._system_prompt = persona + "\n\n" + (self._system_prompt or "")

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})
        self._token_estimate += self._estimate_tokens(content)

    def add_assistant(self, content: str) -> None:
        self._messages.append({"role": "assistant", "content": content})
        self._token_estimate += self._estimate_tokens(content)

    def add_system(self, content: str) -> None:
        """Insert an extra inline system message (tool result, observation)."""
        self._messages.append({"role": "system", "content": content})
        self._token_estimate += self._estimate_tokens(content)

    def add_tool_result(self, tool_name: str, result: str) -> None:
        """Record a tool call result as a system-style message."""
        self.add_system(f"[Tool: {tool_name}]\n{result}")

    def clear(self) -> None:
        """Clear message history (keep system prompt)."""
        self._messages.clear()
        self._token_estimate = 0

    def get_messages(self) -> list[dict[str, str]]:
        """Return the full message list ready to send to an LLM API."""
        out = []
        if self._system_prompt:
            out.append({"role": "system", "content": self._system_prompt})
        out.extend(self._messages)
        return out

    def token_count(self) -> int:
        sys_tokens = self._estimate_tokens(self._system_prompt)
        return sys_tokens + self._token_estimate

    # ------------------------------------------------------------------
    # Feature #59 — Context window compression
    # ------------------------------------------------------------------

    def set_compress_fn(self, fn: Any) -> None:
        """Set an async LLM callable used to summarise history.

        fn signature: async (messages: list[dict]) -> str
        """
        self._compress_fn = fn

    async def maybe_compress(self, token_limit: int | None = None) -> bool:
        """Compress history if we are near the token limit.

        Returns True if compression was performed.
        """
        limit = token_limit or self.max_tokens
        if self.token_count() < limit * 0.8:
            return False
        return await self.compress()

    async def compress(self) -> bool:
        """Summarise the current message history to reduce token usage.

        Uses the injected compress_fn if available, otherwise applies
        a simple truncation heuristic (keep last N messages).
        """
        if not self._messages:
            return False

        if self._compress_fn:
            try:
                summary = await self._compress_fn(self._messages)
                keep_from = max(0, len(self._messages) - 4)  # keep last 4 turns
                self._messages = [
                    {"role": "system", "content": f"[Context summary]\n{summary}"},
                    *self._messages[keep_from:],
                ]
                self._token_estimate = sum(self._estimate_tokens(m["content"]) for m in self._messages)
                logger.info("ContextManager[%s]: compressed history → ~%d tokens", self.agent_id, self.token_count())
                return True
            except Exception as exc:
                logger.warning("ContextManager: compression LLM call failed: %s", exc)

        # Fallback: keep only the last 6 messages
        if len(self._messages) > 6:
            dropped = len(self._messages) - 6
            self._messages = self._messages[-6:]
            self._token_estimate = sum(self._estimate_tokens(m["content"]) for m in self._messages)
            logger.info("ContextManager[%s]: truncated %d messages (fallback)", self.agent_id, dropped)
            return True
        return False

    # ------------------------------------------------------------------
    # Feature #61 — Cross-agent context sharing
    # ------------------------------------------------------------------

    def publish_to_namespace(self, namespace: str) -> None:
        """Write current messages to a shared namespace so other agents can read them."""
        _SHARED_NAMESPACES[namespace] = list(self._messages)
        logger.debug("ContextManager[%s]: published %d messages to namespace '%s'",
                     self.agent_id, len(self._messages), namespace)

    def subscribe_from_namespace(self, namespace: str, limit: int = 10) -> int:
        """Pull the latest messages from a shared namespace into this context.

        Parameters
        ----------
        namespace:
            Namespace key (e.g. 'group-research-team').
        limit:
            Maximum number of messages to import.

        Returns
        -------
        int
            Number of messages imported.
        """
        messages = _SHARED_NAMESPACES.get(namespace, [])
        to_import = messages[-limit:]
        if not to_import:
            return 0
        self._messages.extend(to_import)
        self._token_estimate += sum(self._estimate_tokens(m["content"]) for m in to_import)
        logger.debug("ContextManager[%s]: imported %d messages from namespace '%s'",
                     self.agent_id, len(to_import), namespace)
        return len(to_import)

    @classmethod
    def list_namespaces(cls) -> list[str]:
        return list(_SHARED_NAMESPACES.keys())

    @classmethod
    def clear_namespace(cls, namespace: str) -> bool:
        if namespace in _SHARED_NAMESPACES:
            del _SHARED_NAMESPACES[namespace]
            return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Fast token estimate: ~4 chars per token."""
        return max(1, len(text) // 4)

    def snapshot(self) -> dict[str, Any]:
        """Return a serialisable snapshot of the current context."""
        return {
            "agent_id": self.agent_id,
            "model": self.model,
            "system_prompt": self._system_prompt,
            "messages": list(self._messages),
            "token_estimate": self.token_count(),
            "timestamp": time.time(),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "ContextManager":
        """Restore a ContextManager from a snapshot dict."""
        obj = cls(agent_id=data["agent_id"], model=data.get("model", "gpt-4o"))
        obj._system_prompt = data.get("system_prompt", "")
        obj._messages = data.get("messages", [])
        obj._token_estimate = sum(obj._estimate_tokens(m.get("content", "")) for m in obj._messages)
        return obj

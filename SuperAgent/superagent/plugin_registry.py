"""Tool/Plugin Registry per agent.

Feature #64 — AIOS-inspired per-agent tool registry.

Each agent can register, discover, and invoke tools (plugins) by name.
Tools are isolated per agent — agent-1 may have "web_search" while
agent-2 only has "file_manager".

Includes:
  - Tool registration (sync and async callables)
  - Input schema validation (JSON Schema)
  - Tool discovery / listing
  - Safe invocation with timeout and error capture
  - Plugin manifest loading from YAML files

Usage::

    registry = PluginRegistry(agent_id="agent-1")
    registry.register(
        name="web_search",
        fn=my_search_fn,
        description="Search the web",
        input_schema={"query": "string"},
    )

    result = await registry.invoke("web_search", {"query": "hello world"})
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool model
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """Describes a registered tool."""

    name: str
    fn: Callable[..., Any]
    description: str = ""
    input_schema: dict[str, str] = field(default_factory=dict)
    is_async: bool = False
    timeout_seconds: float = 30.0
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    """Result from invoking a tool."""

    tool_name: str
    success: bool
    output: Any
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Plugin Registry
# ---------------------------------------------------------------------------

class PluginRegistry:
    """Per-agent tool/plugin registry with safe invocation."""

    def __init__(self, agent_id: str, plugins_dir: str | Path = "plugins") -> None:
        self.agent_id = agent_id
        self.plugins_dir = Path(plugins_dir)
        self._tools: dict[str, ToolSpec] = {}
        self._invocation_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        description: str = "",
        input_schema: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
        version: str = "1.0.0",
        tags: list[str] | None = None,
    ) -> None:
        """Register a tool function by name."""
        is_async = asyncio.iscoroutinefunction(fn)
        spec = ToolSpec(
            name=name,
            fn=fn,
            description=description,
            input_schema=input_schema or {},
            is_async=is_async,
            timeout_seconds=timeout_seconds,
            version=version,
            tags=tags or [],
        )
        self._tools[name] = spec
        logger.debug("PluginRegistry[%s]: registered tool '%s'", self.agent_id, name)

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def list_tools(self) -> list[dict[str, Any]]:
        """Return metadata for all registered tools."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "version": t.version,
                "tags": t.tags,
            }
            for t in self._tools.values()
        ]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    async def invoke(self, name: str, inputs: dict[str, Any]) -> ToolResult:
        """Invoke a registered tool safely with timeout."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(tool_name=name, success=False, output=None,
                              error=f"Tool '{name}' not found in registry")

        self._validate_inputs(spec, inputs)
        t0 = time.monotonic()
        try:
            if spec.is_async:
                output = await asyncio.wait_for(spec.fn(**inputs), timeout=spec.timeout_seconds)
            else:
                output = await asyncio.wait_for(
                    asyncio.to_thread(spec.fn, **inputs), timeout=spec.timeout_seconds
                )
            duration_ms = (time.monotonic() - t0) * 1000
            result = ToolResult(tool_name=name, success=True, output=output, duration_ms=duration_ms)
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - t0) * 1000
            result = ToolResult(tool_name=name, success=False, output=None,
                                error=f"Tool '{name}' timed out after {spec.timeout_seconds}s",
                                duration_ms=duration_ms)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            result = ToolResult(tool_name=name, success=False, output=None,
                                error=str(exc), duration_ms=duration_ms)

        self._invocation_log.append({
            "tool": name, "success": result.success,
            "duration_ms": result.duration_ms, "timestamp": result.timestamp,
        })
        return result

    # ------------------------------------------------------------------
    # Plugin manifest loading
    # ------------------------------------------------------------------

    def load_from_directory(self, directory: str | Path | None = None) -> int:
        """Load plugins from Python files in a directory.

        Each plugin file must expose a `PLUGIN_MANIFEST` dict::

            PLUGIN_MANIFEST = {
                "name": "web_search",
                "description": "Search the web",
                "fn": search_function,
                "input_schema": {"query": "string"},
                "tags": ["web", "search"],
            }
        """
        directory = Path(directory or self.plugins_dir)
        if not directory.exists():
            return 0
        count = 0
        for py_file in directory.glob("*.py"):
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    manifest = getattr(mod, "PLUGIN_MANIFEST", None)
                    if manifest and isinstance(manifest, dict):
                        self.register(**manifest)
                        count += 1
            except Exception as exc:
                logger.warning("PluginRegistry: failed to load %s: %s", py_file, exc)
        logger.info("PluginRegistry[%s]: loaded %d plugins from %s", self.agent_id, count, directory)
        return count

    def load_from_yaml(self, yaml_path: str | Path) -> int:
        """Load plugin configuration from a YAML manifest file."""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — cannot load YAML plugin manifests")
            return 0

        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            return 0

        count = 0
        with open(yaml_path, encoding="utf-8") as f:
            manifests = yaml.safe_load(f) or []

        for entry in manifests:
            try:
                module_path = entry.get("module", "")
                fn_name = entry.get("function", "")
                if module_path and fn_name:
                    mod = importlib.import_module(module_path)
                    fn = getattr(mod, fn_name)
                    self.register(
                        name=entry["name"],
                        fn=fn,
                        description=entry.get("description", ""),
                        input_schema=entry.get("input_schema", {}),
                        timeout_seconds=entry.get("timeout_seconds", 30.0),
                        version=entry.get("version", "1.0.0"),
                        tags=entry.get("tags", []),
                    )
                    count += 1
            except Exception as exc:
                logger.warning("PluginRegistry: failed to load YAML entry %s: %s", entry, exc)
        return count

    # ------------------------------------------------------------------
    # Stats & logging
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "total_tools": len(self._tools),
            "total_invocations": len(self._invocation_log),
            "tools": self.list_tools(),
            "recent_invocations": self._invocation_log[-20:],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_inputs(self, spec: ToolSpec, inputs: dict[str, Any]) -> None:
        """Warn (don't raise) if required schema keys are missing."""
        for key, expected_type in spec.input_schema.items():
            if key not in inputs:
                logger.warning(
                    "PluginRegistry: tool '%s' missing required input '%s' (expected %s)",
                    spec.name, key, expected_type,
                )


# ---------------------------------------------------------------------------
# Global registry helper
# ---------------------------------------------------------------------------

_registries: dict[str, PluginRegistry] = {}


def get_registry(agent_id: str) -> PluginRegistry:
    """Get or create the plugin registry for an agent."""
    if agent_id not in _registries:
        _registries[agent_id] = PluginRegistry(agent_id=agent_id)
    return _registries[agent_id]

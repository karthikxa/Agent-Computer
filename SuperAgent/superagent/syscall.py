"""Syscall dispatcher for agent-to-OS requests.

Feature #67 — AIOS-inspired syscall abstraction layer.

Provides a unified dispatcher that routes agent "system calls" to the
appropriate OS-level handler. This decouples agent logic from OS details
and enables policy enforcement, auditing, and sandboxing at a single point.

Supported syscall categories
-----------------------------
  - FILE   : read, write, list, delete, stat
  - PROC   : spawn, kill, list_processes
  - NET    : http_get, http_post, dns_lookup
  - SCREEN : screenshot, get_resolution
  - CLIP   : get_clipboard, set_clipboard
  - SYS    : get_env, set_env, sleep, get_time

Usage::

    dispatcher = SyscallDispatcher(agent_id="agent-1")
    result = await dispatcher.call("FILE.read", {"path": "/tmp/foo.txt"})
    result = await dispatcher.call("NET.http_get", {"url": "https://example.com"})
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Syscall result
# ---------------------------------------------------------------------------

@dataclass
class SyscallResult:
    """Result of a syscall dispatch."""

    syscall: str
    success: bool
    data: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass
class SyscallPolicy:
    """Controls which syscall categories an agent may use."""

    allow_file_read: bool = True
    allow_file_write: bool = True
    allow_file_delete: bool = False
    allow_proc_spawn: bool = True
    allow_proc_kill: bool = False
    allow_net: bool = True
    allow_screen: bool = True
    allow_clipboard: bool = True
    allow_sys: bool = True
    allowed_net_domains: list[str] = field(default_factory=list)  # empty = all allowed
    max_file_size_bytes: int = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class SyscallDispatcher:
    """Routes agent syscall requests to OS handlers with policy enforcement."""

    def __init__(
        self,
        agent_id: str,
        policy: SyscallPolicy | None = None,
        work_dir: str | Path | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.policy = policy or SyscallPolicy()
        self.work_dir = Path(work_dir or f"/tmp/agent-{agent_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._audit: list[dict[str, Any]] = []
        self._handlers: dict[str, Callable[..., Any]] = self._build_handlers()

    # ------------------------------------------------------------------
    # Main dispatch entry point
    # ------------------------------------------------------------------

    async def call(self, syscall: str, params: dict[str, Any] | None = None) -> SyscallResult:
        """Dispatch a syscall by name.

        syscall format: "CATEGORY.action"
        e.g. "FILE.read", "NET.http_get", "SCREEN.screenshot"
        """
        params = params or {}
        t0 = time.monotonic()

        handler = self._handlers.get(syscall)
        if handler is None:
            return SyscallResult(
                syscall=syscall, success=False,
                error=f"Unknown syscall '{syscall}'",
                duration_ms=0.0,
            )

        # Policy check
        ok, reason = self._check_policy(syscall, params)
        if not ok:
            self._audit_entry(syscall, params, success=False, error=f"BLOCKED: {reason}")
            return SyscallResult(syscall=syscall, success=False, error=f"Policy blocked: {reason}")

        try:
            if asyncio.iscoroutinefunction(handler):
                data = await handler(**params)
            else:
                data = await asyncio.to_thread(handler, **params)
            duration_ms = (time.monotonic() - t0) * 1000
            self._audit_entry(syscall, params, success=True)
            return SyscallResult(syscall=syscall, success=True, data=data, duration_ms=duration_ms)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            self._audit_entry(syscall, params, success=False, error=str(exc))
            return SyscallResult(syscall=syscall, success=False, error=str(exc), duration_ms=duration_ms)

    # ------------------------------------------------------------------
    # Handler implementations
    # ------------------------------------------------------------------

    def _build_handlers(self) -> dict[str, Callable[..., Any]]:
        return {
            # FILE
            "FILE.read":    self._file_read,
            "FILE.write":   self._file_write,
            "FILE.list":    self._file_list,
            "FILE.delete":  self._file_delete,
            "FILE.stat":    self._file_stat,
            # PROC
            "PROC.spawn":   self._proc_spawn,
            "PROC.kill":    self._proc_kill,
            "PROC.list":    self._proc_list,
            # NET
            "NET.http_get":  self._net_http_get,
            "NET.http_post": self._net_http_post,
            "NET.dns_lookup": self._net_dns_lookup,
            # SCREEN
            "SCREEN.screenshot":    self._screen_screenshot,
            "SCREEN.get_resolution": self._screen_resolution,
            # CLIPBOARD
            "CLIP.get": self._clip_get,
            "CLIP.set": self._clip_set,
            # SYS
            "SYS.get_env":  self._sys_get_env,
            "SYS.set_env":  self._sys_set_env,
            "SYS.sleep":    self._sys_sleep,
            "SYS.get_time": self._sys_get_time,
        }

    def _file_read(self, path: str, encoding: str = "utf-8") -> str:
        p = self._safe_path(path)
        if p.stat().st_size > self.policy.max_file_size_bytes:
            raise ValueError(f"File too large: {p.stat().st_size} bytes")
        return p.read_text(encoding=encoding)

    def _file_write(self, path: str, content: str, encoding: str = "utf-8") -> bool:
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        return True

    def _file_list(self, path: str = ".") -> list[str]:
        p = self._safe_path(path)
        return [str(f.relative_to(self.work_dir)) for f in p.iterdir()]

    def _file_delete(self, path: str) -> bool:
        p = self._safe_path(path)
        if p.is_file():
            p.unlink()
            return True
        return False

    def _file_stat(self, path: str) -> dict[str, Any]:
        p = self._safe_path(path)
        s = p.stat()
        return {"size": s.st_size, "mtime": s.st_mtime, "is_file": p.is_file(), "is_dir": p.is_dir()}

    async def _proc_spawn(self, command: str, timeout: float = 30.0) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.work_dir),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:4096],
            "stderr": stderr.decode("utf-8", errors="replace")[:4096],
        }

    def _proc_kill(self, pid: int) -> bool:
        import signal
        os.kill(pid, signal.SIGTERM)
        return True

    def _proc_list(self) -> list[dict[str, Any]]:
        try:
            import psutil
            return [{"pid": p.pid, "name": p.name(), "status": p.status()} for p in psutil.process_iter()]
        except ImportError:
            return []

    async def _net_http_get(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                return {"status": resp.status, "body": text[:8192], "headers": dict(resp.headers)}

    async def _net_http_post(self, url: str, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                return {"status": resp.status, "body": text[:8192]}

    async def _net_dns_lookup(self, hostname: str) -> list[str]:
        import socket
        result = await asyncio.get_event_loop().getaddrinfo(hostname, None)
        return list({r[4][0] for r in result})

    async def _screen_screenshot(self) -> str:
        """Return base64-encoded screenshot."""
        import base64
        try:
            import pyautogui
            import io
            from PIL import Image
            img = await asyncio.to_thread(pyautogui.screenshot)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return ""

    def _screen_resolution(self) -> dict[str, int]:
        try:
            import pyautogui
            w, h = pyautogui.size()
            return {"width": w, "height": h}
        except Exception:
            return {"width": 1920, "height": 1080}

    async def _clip_get(self) -> str:
        try:
            import pyperclip
            return await asyncio.to_thread(pyperclip.paste)
        except Exception:
            return ""

    async def _clip_set(self, text: str) -> bool:
        try:
            import pyperclip
            await asyncio.to_thread(pyperclip.copy, text)
            return True
        except Exception:
            return False

    def _sys_get_env(self, key: str) -> str:
        return os.environ.get(key, "")

    def _sys_set_env(self, key: str, value: str) -> bool:
        os.environ[key] = value
        return True

    async def _sys_sleep(self, seconds: float) -> bool:
        await asyncio.sleep(min(seconds, 60.0))
        return True

    def _sys_get_time(self) -> float:
        return time.time()

    # ------------------------------------------------------------------
    # Policy checking
    # ------------------------------------------------------------------

    def _check_policy(self, syscall: str, params: dict[str, Any]) -> tuple[bool, str]:
        p = self.policy
        cat = syscall.split(".")[0]
        action = syscall.split(".")[-1] if "." in syscall else ""

        if cat == "FILE":
            if action == "read" and not p.allow_file_read:
                return False, "file read not allowed"
            if action == "write" and not p.allow_file_write:
                return False, "file write not allowed"
            if action == "delete" and not p.allow_file_delete:
                return False, "file delete not allowed"
        elif cat == "PROC":
            if action == "spawn" and not p.allow_proc_spawn:
                return False, "process spawn not allowed"
            if action == "kill" and not p.allow_proc_kill:
                return False, "process kill not allowed"
        elif cat == "NET":
            if not p.allow_net:
                return False, "network access not allowed"
            if p.allowed_net_domains:
                url = params.get("url", "")
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
                if not any(domain.endswith(d) for d in p.allowed_net_domains):
                    return False, f"domain '{domain}' not in allowlist"
        elif cat == "SCREEN" and not p.allow_screen:
            return False, "screen access not allowed"
        elif cat == "CLIP" and not p.allow_clipboard:
            return False, "clipboard access not allowed"
        elif cat == "SYS" and not p.allow_sys:
            return False, "sys access not allowed"

        return True, ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_path(self, path: str) -> Path:
        """Resolve a path relative to work_dir, preventing escapes."""
        resolved = (self.work_dir / path).resolve()
        if not str(resolved).startswith(str(self.work_dir.resolve())):
            raise PermissionError(f"Path escape attempt: {path}")
        return resolved

    def _audit_entry(self, syscall: str, params: dict[str, Any], *, success: bool, error: str = "") -> None:
        self._audit.append({
            "agent_id": self.agent_id,
            "syscall": syscall,
            "params_keys": list(params.keys()),
            "success": success,
            "error": error,
            "timestamp": time.time(),
        })

    def get_audit_log(self, n: int = 100) -> list[dict[str, Any]]:
        return self._audit[-n:]

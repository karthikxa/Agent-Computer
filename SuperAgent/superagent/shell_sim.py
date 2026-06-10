"""Shell simulator per agent — isolated command execution environment.

Feature #66 — AIOS-inspired per-agent shell.

Provides each agent with an isolated command execution environment:
  - Working directory isolation per agent
  - Command allowlist / blocklist enforcement
  - Captured stdout/stderr with timeout
  - Command history per agent
  - Environment variable namespace per agent

Usage::

    shell = AgentShell(agent_id="agent-1", work_dir="/tmp/agent-1")
    result = await shell.run("ls -la")
    print(result.stdout)

    # With timeout and env override
    result = await shell.run("python script.py", timeout=30, env={"MY_VAR": "value"})
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class ShellResult:
    """Result of running a shell command."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    blocked: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.blocked


# ---------------------------------------------------------------------------
# Shell simulator
# ---------------------------------------------------------------------------

class AgentShell:
    """Isolated shell environment for a single agent.

    Parameters
    ----------
    agent_id:
        Unique identifier for the owning agent.
    work_dir:
        Working directory for all commands. Created if it doesn't exist.
    allowed_commands:
        If set, only commands whose base name is in this set are allowed.
    blocked_commands:
        Commands that are always blocked (e.g. 'rm', 'shutdown').
    default_timeout:
        Default timeout in seconds for each command.
    max_output_bytes:
        Truncate stdout/stderr to this size to prevent memory exhaustion.
    """

    # System-level commands always blocked for safety
    _ALWAYS_BLOCKED = frozenset({
        "shutdown", "reboot", "halt", "poweroff",
        "mkfs", "fdisk", "dd",
        "iptables", "ip6tables",
        "passwd", "su", "sudo",
    })

    def __init__(
        self,
        agent_id: str,
        work_dir: str | Path | None = None,
        *,
        allowed_commands: set[str] | None = None,
        blocked_commands: set[str] | None = None,
        default_timeout: float = 60.0,
        max_output_bytes: int = 64 * 1024,  # 64 KB
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.work_dir = Path(work_dir or f"/tmp/agent-shell-{agent_id}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_commands = allowed_commands
        self.blocked_commands = (blocked_commands or set()) | self._ALWAYS_BLOCKED
        self.default_timeout = default_timeout
        self.max_output_bytes = max_output_bytes
        self._env_overrides = env_overrides or {}
        self._history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        cwd: str | Path | None = None,
    ) -> ShellResult:
        """Execute a shell command and return the result.

        Parameters
        ----------
        command:
            Shell command string (run via /bin/sh -c on Linux/macOS,
            cmd /c on Windows).
        timeout:
            Override default timeout (seconds).
        env:
            Additional environment variables (merged with base env).
        stdin:
            Optional string to pass as stdin.
        cwd:
            Override working directory for this command.
        """
        timeout = timeout or self.default_timeout
        run_cwd = Path(cwd) if cwd else self.work_dir

        # Check blocklist
        base_cmd = shlex.split(command)[0] if command.strip() else ""
        if self._is_blocked(base_cmd):
            logger.warning("AgentShell[%s]: blocked command: %s", self.agent_id, base_cmd)
            result = ShellResult(
                command=command, returncode=-1,
                stdout="", stderr=f"Command '{base_cmd}' is blocked by policy.",
                duration_ms=0.0, blocked=True,
            )
            self._log(result)
            return result

        # Build environment
        merged_env = {**os.environ, **self._env_overrides, **(env or {})}

        t0 = time.monotonic()
        timed_out = False
        stdout_str = ""
        stderr_str = ""
        returncode = -1

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin else None,
                cwd=str(run_cwd),
                env=merged_env,
            )
            stdin_bytes = stdin.encode() if stdin else None
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=timeout
                )
                returncode = proc.returncode or 0
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                timed_out = True
                returncode = -1
                stderr_str = f"Command timed out after {timeout}s"
            else:
                stdout_str = stdout_bytes.decode("utf-8", errors="replace")
                stderr_str = stderr_bytes.decode("utf-8", errors="replace")
                # Truncate large outputs
                if len(stdout_str) > self.max_output_bytes:
                    stdout_str = stdout_str[: self.max_output_bytes] + "\n[...output truncated...]"
                if len(stderr_str) > self.max_output_bytes:
                    stderr_str = stderr_str[: self.max_output_bytes] + "\n[...truncated...]"

        except Exception as exc:
            stderr_str = str(exc)
            returncode = -1

        duration_ms = (time.monotonic() - t0) * 1000
        result = ShellResult(
            command=command,
            returncode=returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
        self._log(result)
        return result

    async def run_script(self, script: str, interpreter: str = "bash") -> ShellResult:
        """Write a multi-line script to a temp file and execute it."""
        import tempfile
        script_file = self.work_dir / f"_script_{int(time.time())}.sh"
        script_file.write_text(script, encoding="utf-8")
        script_file.chmod(0o700)
        try:
            return await self.run(f"{interpreter} {script_file}")
        finally:
            script_file.unlink(missing_ok=True)

    def set_env(self, key: str, value: str) -> None:
        """Set a persistent environment variable for this agent shell."""
        self._env_overrides[key] = value

    def get_env(self) -> dict[str, str]:
        return dict(self._env_overrides)

    def history(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the last n commands run by this agent."""
        return self._history[-n:]

    def clear_history(self) -> None:
        self._history.clear()

    def reset_workdir(self) -> None:
        """Wipe and recreate the working directory (ephemeral reset)."""
        import shutil
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_blocked(self, cmd: str) -> bool:
        if not cmd:
            return False
        base = Path(cmd).name
        if base in self.blocked_commands:
            return True
        if self.allowed_commands and base not in self.allowed_commands:
            return True
        return False

    def _log(self, result: ShellResult) -> None:
        self._history.append({
            "command": result.command,
            "returncode": result.returncode,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "blocked": result.blocked,
            "timestamp": time.time(),
        })

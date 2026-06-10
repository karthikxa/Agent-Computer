"""App install and launch manager per agent container.

Feature #74 — inspired by e2b/open-computer-use.

Manages application installation and launching inside agent containers:
  - Package installation via apt/pip/npm/snap
  - Application launching with process tracking
  - Process health monitoring and restart
  - App catalogue (known apps with install commands)

Usage::

    manager = AppManager(agent_id="agent-1")
    result = await manager.install("chromium-browser", method="apt")
    proc = await manager.launch("chromium-browser", args=["--no-sandbox"])
    await manager.stop("chromium-browser")
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class AppProcess:
    """A running application process managed by AppManager."""
    app_name: str
    pid: int
    launched_at: float
    args: list[str] = field(default_factory=list)
    _proc: Any = field(default=None, repr=False)

    @property
    def is_running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.returncode is None


@dataclass
class InstallResult:
    """Result of an app installation attempt."""
    app_name: str
    method: str
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# App Catalogue
# ---------------------------------------------------------------------------

APP_CATALOGUE: dict[str, dict[str, Any]] = {
    "chromium-browser": {
        "apt": "chromium-browser",
        "snap": "chromium",
        "binary": "chromium-browser",
        "launch_args": ["--no-sandbox", "--disable-gpu"],
    },
    "firefox": {
        "apt": "firefox",
        "snap": "firefox",
        "binary": "firefox",
        "launch_args": [],
    },
    "vscode": {
        "snap": "code --classic",
        "binary": "code",
        "launch_args": [],
    },
    "libreoffice": {
        "apt": "libreoffice",
        "binary": "libreoffice",
        "launch_args": [],
    },
    "gimp": {
        "apt": "gimp",
        "binary": "gimp",
        "launch_args": [],
    },
    "python3": {
        "apt": "python3",
        "binary": "python3",
        "launch_args": [],
    },
    "nodejs": {
        "apt": "nodejs",
        "binary": "node",
        "launch_args": [],
    },
}


# ---------------------------------------------------------------------------
# App Manager
# ---------------------------------------------------------------------------

class AppManager:
    """Install and launch applications within an agent container."""

    def __init__(self, agent_id: str, display: str = ":1") -> None:
        self.agent_id = agent_id
        self.display = display
        self._processes: dict[str, AppProcess] = {}

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    async def install(
        self,
        app_name: str,
        method: str = "auto",
        *,
        package_name: str | None = None,
        timeout: float = 120.0,
    ) -> InstallResult:
        """Install an application using the specified package manager.

        Parameters
        ----------
        app_name:
            Logical app name (e.g. 'chromium-browser').
        method:
            'apt' | 'pip' | 'npm' | 'snap' | 'auto'
        package_name:
            Override the package name (defaults to catalogue lookup).
        timeout:
            Install timeout in seconds.
        """
        t0 = time.monotonic()
        catalogue = APP_CATALOGUE.get(app_name, {})

        if method == "auto":
            method = self._detect_method(catalogue)

        pkg = package_name or catalogue.get(method, app_name)
        cmd = self._build_install_cmd(method, pkg)
        if not cmd:
            return InstallResult(
                app_name=app_name, method=method, success=False,
                error=f"Unknown install method: {method}"
            )

        logger.info("AppManager[%s]: installing '%s' via %s", self.agent_id, pkg, method)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            success = proc.returncode == 0
            duration_ms = (time.monotonic() - t0) * 1000
            return InstallResult(
                app_name=app_name, method=method, success=success,
                output=stdout.decode("utf-8", errors="replace")[:2048],
                error=stderr.decode("utf-8", errors="replace")[:2048] if not success else "",
                duration_ms=duration_ms,
            )
        except asyncio.TimeoutError:
            return InstallResult(
                app_name=app_name, method=method, success=False,
                error=f"Install timed out after {timeout}s",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            return InstallResult(
                app_name=app_name, method=method, success=False,
                error=str(exc), duration_ms=(time.monotonic() - t0) * 1000,
            )

    # ------------------------------------------------------------------
    # Launch & lifecycle
    # ------------------------------------------------------------------

    async def launch(
        self,
        app_name: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> AppProcess | None:
        """Launch an application in the agent's virtual display."""
        catalogue = APP_CATALOGUE.get(app_name, {})
        binary = catalogue.get("binary", app_name)
        default_args: list[str] = catalogue.get("launch_args", [])
        all_args = (args or []) + (default_args if not args else [])

        if not shutil.which(binary):
            logger.warning("AppManager[%s]: binary '%s' not found", self.agent_id, binary)
            return None

        cmd = [binary] + all_args
        import os
        merged_env = {**os.environ, "DISPLAY": self.display, **(env or {})}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=merged_env,
        )
        app_proc = AppProcess(
            app_name=app_name,
            pid=proc.pid,
            launched_at=time.time(),
            args=all_args,
            _proc=proc,
        )
        self._processes[app_name] = app_proc
        logger.info("AppManager[%s]: launched '%s' PID=%d", self.agent_id, app_name, proc.pid)
        return app_proc

    async def stop(self, app_name: str) -> bool:
        """Stop a running application."""
        app_proc = self._processes.get(app_name)
        if not app_proc or not app_proc.is_running:
            return False
        try:
            app_proc._proc.terminate()
            await asyncio.wait_for(app_proc._proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            app_proc._proc.kill()
        self._processes.pop(app_name, None)
        logger.info("AppManager[%s]: stopped '%s'", self.agent_id, app_name)
        return True

    async def restart(self, app_name: str) -> AppProcess | None:
        """Stop then relaunch an application."""
        app_proc = self._processes.get(app_name)
        args = app_proc.args if app_proc else []
        await self.stop(app_name)
        return await self.launch(app_name, args)

    def is_running(self, app_name: str) -> bool:
        ap = self._processes.get(app_name)
        return ap is not None and ap.is_running

    def list_running(self) -> list[dict[str, Any]]:
        return [
            {
                "app_name": ap.app_name,
                "pid": ap.pid,
                "launched_at": ap.launched_at,
                "is_running": ap.is_running,
            }
            for ap in self._processes.values()
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_method(catalogue: dict[str, Any]) -> str:
        if shutil.which("apt-get") and "apt" in catalogue:
            return "apt"
        if shutil.which("pip3") and "pip" in catalogue:
            return "pip"
        if shutil.which("npm") and "npm" in catalogue:
            return "npm"
        if shutil.which("snap") and "snap" in catalogue:
            return "snap"
        return "apt"

    @staticmethod
    def _build_install_cmd(method: str, package: str) -> str:
        if method == "apt":
            return f"DEBIAN_FRONTEND=noninteractive apt-get install -y {package}"
        if method == "pip":
            return f"pip3 install --quiet {package}"
        if method == "npm":
            return f"npm install -g {package}"
        if method == "snap":
            return f"snap install {package}"
        return ""

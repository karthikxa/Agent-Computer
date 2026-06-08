"""MicroVM container shim for SuperAgent.

Inspired by e2b-dev/open-computer-use's Firecracker microVM sandboxing.

Provides a unified ``SandboxManager`` that abstracts over:
  - **Docker** (existing, default)   — full container lifecycle management.
  - **Firecracker MicroVM**          — sub-second boot, kernel-level isolation.
  - **Process** (lightweight/test)   — runs the agent in a subprocess sandbox.

The same ``SandboxSpec`` / ``SandboxHandle`` API is used regardless of backend,
so callers never need to change their orchestration code when switching modes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public enums / data models
# ---------------------------------------------------------------------------

class SandboxBackend(str, Enum):
    DOCKER = "docker"
    FIRECRACKER = "firecracker"
    PROCESS = "process"


@dataclass
class SandboxSpec:
    """Specification for launching a new agent sandbox."""

    agent_id: str
    image: str = "superagent:latest"          # Docker image or rootfs path
    cpus: float = 1.0
    memory_mb: int = 2048
    desktop_port: int = 8000
    vnc_port: int = 6901
    env: dict[str, str] = field(default_factory=dict)
    backend: SandboxBackend = SandboxBackend.DOCKER
    # Firecracker-specific
    kernel_image: str = "/var/lib/firecracker/vmlinux"
    rootfs: str = "/var/lib/firecracker/rootfs.ext4"
    # Shared volume / workspace
    workspace_dir: Path = field(default_factory=lambda: Path(".superagent/workspace"))


@dataclass
class SandboxHandle:
    """Live reference to a running sandbox."""

    sandbox_id: str
    agent_id: str
    backend: SandboxBackend
    desktop_url: str
    vnc_url: str
    started_at: float = field(default_factory=time.monotonic)
    _process: Any = field(default=None, repr=False)   # subprocess.Popen or docker SDK
    _pid: int | None = None

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

class _DockerBackend:
    """Thin wrapper around the Docker SDK / CLI."""

    async def launch(self, spec: SandboxSpec) -> SandboxHandle:
        container_name = f"superagent-{spec.agent_id}"
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            f"--cpus={spec.cpus}",
            f"--memory={spec.memory_mb}m",
            "-p", f"{spec.desktop_port}:8000",
            "-p", f"{spec.vnc_port}:6901",
            "-v", f"{spec.workspace_dir.resolve()}:/workspace",
        ]
        for k, v in spec.env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(spec.image)
        logger.info("Docker: launching container %s", container_name)
        await asyncio.to_thread(subprocess.run, cmd, check=True,
                                capture_output=True)
        return SandboxHandle(
            sandbox_id=container_name,
            agent_id=spec.agent_id,
            backend=SandboxBackend.DOCKER,
            desktop_url=f"http://127.0.0.1:{spec.desktop_port}",
            vnc_url=f"http://127.0.0.1:{spec.vnc_port}",
        )

    async def stop(self, handle: SandboxHandle) -> None:
        logger.info("Docker: stopping container %s", handle.sandbox_id)
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "rm", "-f", handle.sandbox_id],
            capture_output=True,
        )

    async def exec(self, handle: SandboxHandle, command: str) -> str:
        result = await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", handle.sandbox_id, "sh", "-c", command],
            capture_output=True, text=True,
        )
        return result.stdout


class _FirecrackerBackend:
    """Firecracker MicroVM backend.

    Requires the ``firecracker`` and ``jailer`` binaries to be installed
    and a pre-built kernel + rootfs image. Provides sub-100 ms boot times
    and true kernel isolation.
    """

    _FIRECRACKER_SOCKET = "/tmp/firecracker-{agent_id}.sock"

    async def launch(self, spec: SandboxSpec) -> SandboxHandle:
        if not shutil.which("firecracker"):
            raise RuntimeError(
                "Firecracker binary not found. Install from "
                "https://github.com/firecracker-microvm/firecracker/releases"
            )
        socket_path = self._FIRECRACKER_SOCKET.format(agent_id=spec.agent_id)

        # Launch Firecracker VMM process
        proc = await asyncio.create_subprocess_exec(
            "firecracker",
            "--api-sock", socket_path,
            "--level", "Warning",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.2)  # allow socket to open

        # Configure via the Firecracker REST API
        import aiohttp
        async with aiohttp.UnixConnector(path=socket_path) as conn:
            async with aiohttp.ClientSession(connector=conn) as session:
                # Boot source
                await session.put(
                    "http://localhost/boot-source",
                    json={"kernel_image_path": spec.kernel_image,
                          "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"},
                )
                # Root drive
                await session.put(
                    "http://localhost/drives/rootfs",
                    json={"drive_id": "rootfs", "path_on_host": spec.rootfs,
                          "is_root_device": True, "is_read_only": False},
                )
                # Machine config
                await session.put(
                    "http://localhost/machine-config",
                    json={"vcpu_count": max(1, int(spec.cpus)),
                          "mem_size_mib": spec.memory_mb},
                )
                # Start
                await session.put("http://localhost/actions",
                                  json={"action_type": "InstanceStart"})

        logger.info("Firecracker microVM started for agent %s", spec.agent_id)
        return SandboxHandle(
            sandbox_id=f"fc-{spec.agent_id}",
            agent_id=spec.agent_id,
            backend=SandboxBackend.FIRECRACKER,
            desktop_url=f"http://127.0.0.1:{spec.desktop_port}",
            vnc_url=f"http://127.0.0.1:{spec.vnc_port}",
            _process=proc,
            _pid=proc.pid,
        )

    async def stop(self, handle: SandboxHandle) -> None:
        if handle._process:
            handle._process.terminate()
            try:
                await asyncio.wait_for(handle._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                handle._process.kill()
        socket_path = self._FIRECRACKER_SOCKET.format(agent_id=handle.agent_id)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        logger.info("Firecracker microVM %s stopped", handle.sandbox_id)

    async def exec(self, handle: SandboxHandle, command: str) -> str:
        # Firecracker exec requires vsock or SSH setup — return placeholder
        return f"[firecracker exec not available without vsock config]: {command}"


class _ProcessBackend:
    """Lightweight subprocess sandbox — for local testing without Docker."""

    async def launch(self, spec: SandboxSpec) -> SandboxHandle:
        sandbox_id = f"proc-{spec.agent_id}"
        logger.info("ProcessBackend: launching subprocess sandbox %s", sandbox_id)
        proc = await asyncio.create_subprocess_exec(
            "python", "-c",
            f"import time; print('sandbox {sandbox_id} running'); time.sleep(9999)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return SandboxHandle(
            sandbox_id=sandbox_id,
            agent_id=spec.agent_id,
            backend=SandboxBackend.PROCESS,
            desktop_url=f"http://127.0.0.1:{spec.desktop_port}",
            vnc_url=f"http://127.0.0.1:{spec.vnc_port}",
            _process=proc,
            _pid=proc.pid,
        )

    async def stop(self, handle: SandboxHandle) -> None:
        if handle._process:
            handle._process.terminate()
            try:
                await asyncio.wait_for(handle._process.wait(), timeout=3)
            except asyncio.TimeoutError:
                handle._process.kill()

    async def exec(self, handle: SandboxHandle, command: str) -> str:
        result = await asyncio.to_thread(
            subprocess.run, ["sh", "-c", command],
            capture_output=True, text=True,
        )
        return result.stdout


# ---------------------------------------------------------------------------
# Unified SandboxManager
# ---------------------------------------------------------------------------

class SandboxManager:
    """High-level manager that wraps all sandbox backends.

    Usage::

        mgr = SandboxManager(default_backend=SandboxBackend.DOCKER)
        handle = await mgr.launch(SandboxSpec(agent_id="agent-1"))
        output = await mgr.exec(handle, "echo hello")
        await mgr.stop(handle)
    """

    def __init__(self, default_backend: SandboxBackend = SandboxBackend.DOCKER) -> None:
        self.default_backend = default_backend
        self._handles: dict[str, SandboxHandle] = {}
        self._backends: dict[SandboxBackend, Any] = {
            SandboxBackend.DOCKER: _DockerBackend(),
            SandboxBackend.FIRECRACKER: _FirecrackerBackend(),
            SandboxBackend.PROCESS: _ProcessBackend(),
        }

    async def launch(self, spec: SandboxSpec) -> SandboxHandle:
        """Launch a new sandbox and register it."""
        backend = self._backends[spec.backend or self.default_backend]
        handle = await backend.launch(spec)
        self._handles[handle.sandbox_id] = handle
        logger.info("Sandbox %s (%s) launched in %.1f s",
                    handle.sandbox_id, spec.backend.value, handle.uptime_seconds)
        return handle

    async def stop(self, handle: SandboxHandle) -> None:
        """Stop a sandbox and remove it from the registry."""
        backend = self._backends[handle.backend]
        await backend.stop(handle)
        self._handles.pop(handle.sandbox_id, None)

    async def stop_all(self) -> None:
        """Stop all registered sandboxes."""
        for handle in list(self._handles.values()):
            await self.stop(handle)

    async def exec(self, handle: SandboxHandle, command: str) -> str:
        """Execute a shell command inside a sandbox."""
        backend = self._backends[handle.backend]
        return await backend.exec(handle, command)

    def list_handles(self) -> list[SandboxHandle]:
        """Return all live sandbox handles."""
        return list(self._handles.values())

    def get_handle(self, sandbox_id: str) -> SandboxHandle | None:
        return self._handles.get(sandbox_id)

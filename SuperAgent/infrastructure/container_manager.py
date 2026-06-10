"""Docker container manager for agent scaling."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(slots=True)
class ContainerPorts:
    """Port allocation for one agent container."""

    desktop: int
    vnc: int
    stream: int


class ContainerManager:
    """Manage agent containers with the Docker Python SDK."""

    def __init__(
        self,
        *,
        image: str | None = None,
        base_desktop_port: int = 8000,
        base_vnc_port: int = 6901,
        base_stream_port: int = 7080,
        max_agents: int = 250,
    ) -> None:
        self.image = image or os.getenv("AGENT_IMAGE", "superagent:local")
        self.base_desktop_port = base_desktop_port
        self.base_vnc_port = base_vnc_port
        self.base_stream_port = base_stream_port
        self.max_agents = max_agents
        self._containers: dict[str, Any] = {}

    def _docker(self):
        import docker

        return docker.from_env()

    def _ports(self, agent_id: int) -> ContainerPorts:
        return ContainerPorts(
            desktop=self.base_desktop_port + agent_id,
            vnc=self.base_vnc_port + agent_id,
            stream=self.base_stream_port + agent_id,
        )

    async def spawn(self, agent_id: int) -> dict[str, Any]:
        """Start one agent container and wait for health."""

        if agent_id > self.max_agents:
            raise ValueError(f"agent_id {agent_id} exceeds max_agents={self.max_agents}")

        docker_client = self._docker()
        ports = self._ports(agent_id)
        name = f"superagent-{agent_id}"
        volume_name = f"agent-data-{agent_id}"

        def _create():
            volume = docker_client.volumes.get(volume_name) if volume_name in [v.name for v in docker_client.volumes.list()] else docker_client.volumes.create(name=volume_name)
            container = docker_client.containers.run(
                self.image,
                name=name,
                detach=True,
                environment={
                    "DISPLAY": ":1",
                    "AGENT_ID": str(agent_id),
                },
                ports={
                    "8000/tcp": ports.desktop,
                    "6901/tcp": ports.vnc,
                    "7080/tcp": ports.stream,
                },
                volumes={volume.name: {"bind": "/tmp/agent-data", "mode": "rw"}},
                shm_size="2gb",
            )
            self._containers[str(agent_id)] = container
            return container

        container = await asyncio.to_thread(_create)
        await self._wait_for_health(agent_id)
        return {"agent_id": str(agent_id), "ports": ports.__dict__, "container_id": container.id}

    async def _wait_for_health(self, agent_id: int) -> None:
        """Wait until the desktop API health endpoint returns 200."""

        deadline = asyncio.get_running_loop().time() + 120
        while asyncio.get_running_loop().time() < deadline:
            if await self.health_check(agent_id):
                return
            await asyncio.sleep(2)
        raise TimeoutError(f"agent {agent_id} did not become healthy")

    async def spawn_all(self, n: int) -> list[dict[str, Any]]:
        """Spawn many containers in parallel."""

        return await asyncio.gather(*(self.spawn(i) for i in range(1, n + 1)))

    async def kill(self, agent_id: int) -> None:
        """Stop and remove a container."""

        container = self._containers.get(str(agent_id))
        if not container:
            return

        def _kill():
            try:
                container.stop(timeout=10)
            finally:
                container.remove(v=True, force=True)

        await asyncio.to_thread(_kill)
        self._containers.pop(str(agent_id), None)

    async def kill_all(self) -> None:
        """Kill all containers."""

        await asyncio.gather(*(self.kill(int(agent_id)) for agent_id in list(self._containers)))

    async def restart(self, agent_id: int) -> dict[str, Any]:
        """Restart a container."""

        await self.kill(agent_id)
        return await self.spawn(agent_id)

    async def health_check(self, agent_id: int) -> bool:
        """Check the /health endpoint of one container."""

        ports = self._ports(agent_id)
        url = f"http://127.0.0.1:{ports.desktop}/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def health_check_all(self) -> dict[str, bool]:
        """Check all active containers."""

        return {agent_id: await self.health_check(int(agent_id)) for agent_id in self._containers}

    def list_running(self) -> list[dict[str, Any]]:
        """Return all active agent IDs and ports."""

        return [
            {"agent_id": agent_id, "ports": self.get_ports(int(agent_id))}
            for agent_id in sorted(self._containers, key=int)
        ]

    def get_ports(self, agent_id: int) -> dict[str, str]:
        """Return URLs for one agent."""

        ports = self._ports(agent_id)
        return {
            "desktop": f"http://127.0.0.1:{ports.desktop}",
            "vnc": f"http://127.0.0.1:{ports.vnc}",
            "stream": f"http://127.0.0.1:{ports.stream}/index.m3u8",
        }

    # ------------------------------------------------------------------
    # Feature #94 — Per-agent ephemeral storage (wipe after task)
    # ------------------------------------------------------------------

    async def wipe_agent_storage(self, agent_id: int) -> bool:
        """Wipe the ephemeral storage volume for an agent after task completion.

        Deletes the Docker volume ``agent-data-{agent_id}`` and recreates
        it empty, so the next task starts with a clean slate.
        """
        volume_name = f"agent-data-{agent_id}"

        def _wipe() -> bool:
            docker_client = self._docker()
            try:
                vol = docker_client.volumes.get(volume_name)
                vol.remove(force=True)
            except Exception:
                pass  # Volume may not exist yet — that's fine
            docker_client.volumes.create(name=volume_name)
            return True

        result = await asyncio.to_thread(_wipe)
        if result:
            import logging
            logging.getLogger(__name__).info(
                "Ephemeral storage wiped for agent-%d", agent_id
            )
        return result

    async def wipe_all_storage(self) -> None:
        """Wipe ephemeral storage for all managed agents."""
        await asyncio.gather(
            *(self.wipe_agent_storage(int(aid)) for aid in self._containers)
        )

    # ------------------------------------------------------------------
    # Feature #63 — Per-agent storage quota management
    # ------------------------------------------------------------------

    async def set_storage_quota(self, agent_id: int, size_gb: float = 10.0) -> dict[str, Any]:
        """Apply a storage quota to an agent's container via Docker --storage-opt.

        Note: requires an overlay2 / devicemapper storage driver that
        supports ``--storage-opt size=``.  Falls back to a soft warning
        if the driver does not support it.
        """
        container = self._containers.get(str(agent_id))
        if container is None:
            return {"success": False, "error": f"Agent {agent_id} not running"}

        def _apply_quota():
            try:
                # Docker SDK: update resource constraints
                container.update(storage_opt={"size": f"{size_gb}G"})
                return {"success": True, "agent_id": agent_id, "quota_gb": size_gb}
            except Exception as exc:
                return {"success": False, "error": str(exc), "note": "Storage quota requires overlay2 driver"}

        return await asyncio.to_thread(_apply_quota)

    async def get_storage_usage(self, agent_id: int) -> dict[str, Any]:
        """Return disk usage for one agent container."""
        container = self._containers.get(str(agent_id))
        if container is None:
            return {"agent_id": agent_id, "error": "not running"}

        def _usage():
            try:
                stats = container.stats(stream=False)
                return {
                    "agent_id": agent_id,
                    "blk_read_bytes":  stats.get("blkio_stats", {}).get("io_service_bytes_recursive", [{}])[0].get("value", 0),
                    "blk_write_bytes": stats.get("blkio_stats", {}).get("io_service_bytes_recursive", [{}])[-1].get("value", 0),
                }
            except Exception as exc:
                return {"agent_id": agent_id, "error": str(exc)}

        return await asyncio.to_thread(_usage)

    # ------------------------------------------------------------------
    # Feature #95 — Multi-GPU load balancing across agent containers
    # ------------------------------------------------------------------

    def _get_available_gpus(self) -> list[int]:
        """Return list of available GPU indices from nvidia-smi."""
        import subprocess
        import shutil
        if not shutil.which("nvidia-smi"):
            return []
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
        except Exception:
            return []

    def _assign_gpu(self, agent_id: int) -> int | None:
        """Round-robin GPU assignment for an agent."""
        gpus = self._get_available_gpus()
        if not gpus:
            return None
        return gpus[agent_id % len(gpus)]

    async def spawn_with_gpu(self, agent_id: int) -> dict[str, Any]:
        """Spawn one agent container pinned to a GPU (feature #95).

        Uses NVIDIA Container Toolkit device assignment via
        ``--device /dev/nvidia{N}``.
        """
        if agent_id > self.max_agents:
            raise ValueError(f"agent_id {agent_id} exceeds max_agents={self.max_agents}")

        docker_client = self._docker()
        ports = self._ports(agent_id)
        name = f"superagent-{agent_id}"
        volume_name = f"agent-data-{agent_id}"
        gpu_index = self._assign_gpu(agent_id)

        def _create():
            volumes = docker_client.volumes.list()
            vol_names = [v.name for v in volumes]
            volume = (
                docker_client.volumes.get(volume_name)
                if volume_name in vol_names
                else docker_client.volumes.create(name=volume_name)
            )
            device_requests = []
            if gpu_index is not None:
                from docker.types import DeviceRequest
                device_requests = [DeviceRequest(device_ids=[str(gpu_index)], capabilities=[["gpu"]])]

            container = docker_client.containers.run(
                self.image,
                name=name,
                detach=True,
                environment={"DISPLAY": ":1", "AGENT_ID": str(agent_id), "NVIDIA_VISIBLE_DEVICES": str(gpu_index or "none")},
                ports={"8000/tcp": ports.desktop, "6901/tcp": ports.vnc, "7080/tcp": ports.stream},
                volumes={volume.name: {"bind": "/tmp/agent-data", "mode": "rw"}},
                shm_size="2gb",
                device_requests=device_requests or None,
            )
            self._containers[str(agent_id)] = container
            return container

        container = await asyncio.to_thread(_create)
        await self._wait_for_health(agent_id)
        return {
            "agent_id": str(agent_id),
            "ports": self._ports(agent_id).__dict__,
            "container_id": container.id,
            "gpu_index": gpu_index,
        }

    # ------------------------------------------------------------------
    # Feature #96 — ARM64 / Graviton support
    # ------------------------------------------------------------------

    async def spawn_arm64(self, agent_id: int, arm_image: str | None = None) -> dict[str, Any]:
        """Spawn an agent container on ARM64/Graviton instances.

        Uses ``platform=linux/arm64`` in the Docker run call, and an
        optional ARM-specific image override.
        """
        if agent_id > self.max_agents:
            raise ValueError(f"agent_id {agent_id} exceeds max_agents={self.max_agents}")

        image = arm_image or os.getenv("AGENT_ARM_IMAGE", self.image + "-arm64")
        docker_client = self._docker()
        ports = self._ports(agent_id)
        name = f"superagent-arm-{agent_id}"

        def _create():
            container = docker_client.containers.run(
                image,
                name=name,
                detach=True,
                platform="linux/arm64",
                environment={"DISPLAY": ":1", "AGENT_ID": str(agent_id), "ARCH": "arm64"},
                ports={"8000/tcp": ports.desktop, "6901/tcp": ports.vnc, "7080/tcp": ports.stream},
                shm_size="2gb",
            )
            self._containers[str(agent_id)] = container
            return container

        container = await asyncio.to_thread(_create)
        await self._wait_for_health(agent_id)
        return {
            "agent_id": str(agent_id),
            "ports": ports.__dict__,
            "container_id": container.id,
            "platform": "linux/arm64",
        }


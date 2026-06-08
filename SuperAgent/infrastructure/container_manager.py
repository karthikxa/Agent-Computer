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

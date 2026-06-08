"""Top-level SuperAgent orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import ActionExecutor
from .config import AgentConfig
from .cost_tracker import CostTracker
from .desktop_api import DesktopAPI
from .grounding import CoordinateGrounding, GroundingModel, OSAtlasGrounding
from .loop import AgentLoop
from .memory import AgentMemory, MemoryRecord
from .monitor import WatchdogManager
from .providers import create_provider
from .queue import PriorityTaskQueue
from .scheduler import TaskScheduler
from .session import SessionManager
from .stream import StreamConfig, StreamManager
from .verification import HumanVerificationHandler
from .escalation import EscalationManager
from .security import SecurityManager, SecurityConfig
from .dashboard_api import DashboardAPIServer


@dataclass
class AgentRuntime:
    """Aggregated runtime components."""

    config: AgentConfig
    desktop_api: DesktopAPI
    provider: Any
    action_executor: ActionExecutor
    loop: AgentLoop
    stream: StreamManager
    monitor: WatchdogManager
    scheduler: TaskScheduler
    session_manager: SessionManager
    memory: AgentMemory | None = None
    queue: PriorityTaskQueue = field(default_factory=PriorityTaskQueue)
    cost_tracker: CostTracker = field(default_factory=CostTracker)
    escalation: EscalationManager | None = None
    verification: HumanVerificationHandler | None = None


class SuperAgent:
    """Composable computer-use agent runtime."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.desktop_api = DesktopAPI(host=config.desktop_host, port=config.desktop_port)
        provider_name = config.provider
        model_name = config.action_model or config.model or config.vision_model
        vision_model = (config.vision_model or "").lower()
        if vision_model.startswith("claude"):
            provider_name = "anthropic"
            model_name = config.vision_model
        elif vision_model.startswith("gpt"):
            provider_name = "openai"
            model_name = config.vision_model
        elif vision_model == "ollama" or vision_model.startswith("llava"):
            provider_name = "ollama"
            model_name = config.action_model or "llava"
        self.provider = create_provider(provider_name, model_name, api_key=config.api_key, base_url=config.base_url or config.api_base_url)
        self.action_executor = ActionExecutor(self.desktop_api)
        grounding: GroundingModel = OSAtlasGrounding() if config.provider == "osatlas" else CoordinateGrounding()
        self.loop = AgentLoop(
            self.provider,
            self.action_executor,
            self.desktop_api,
            grounding=grounding,
            stuck_threshold=config.stuck_threshold,
            max_steps=config.max_steps,
        )
        self.stream = StreamManager(StreamConfig(host=config.stream_host, vnc_port=config.stream_port, hls_port=config.hls_port, stream_dir=config.stream_dir))
        self.monitor = WatchdogManager(heartbeat_interval_seconds=config.heartbeat_interval_seconds)
        self.scheduler = TaskScheduler()
        self.session_manager = SessionManager(config.session_dir)
        self.memory = AgentMemory(config.memory_db_path) if config.enable_memory else None
        self.escalation = EscalationManager(config.escalation_webhook_url) if config.enable_escalation else None
        self.verification = HumanVerificationHandler(
            totp_secrets=config.totp_secrets,
            email_address=config.verification_email,
            imap_host=config.imap_host,
            imap_user=config.imap_user,
            imap_app_password=config.imap_app_password,
        )
        self.security_manager = SecurityManager()
        self.dashboard_api = DashboardAPIServer(agent=self)
        self.runtime = AgentRuntime(
            config=config,
            desktop_api=self.desktop_api,
            provider=self.provider,
            action_executor=self.action_executor,
            loop=self.loop,
            stream=self.stream,
            monitor=self.monitor,
            scheduler=self.scheduler,
            session_manager=self.session_manager,
            memory=self.memory,
            escalation=self.escalation,
            verification=self.verification,
        )

    async def start(self) -> None:
        """Start auxiliary systems."""

        await self.stream.start()
        await self.dashboard_api.start()
        if self.config.enable_monitor:
            await self.monitor.start()

    async def stop(self) -> None:
        """Stop auxiliary systems."""

        if self.config.enable_monitor:
            await self.monitor.stop()
        await self.dashboard_api.stop()
        await self.stream.stop()
        await self.desktop_api.close()

    async def run(self, objective: str) -> list[Any]:
        """Run the agent loop for one objective."""

        await self.start()
        return await self.loop.run(objective)

    def pause(self) -> None:
        """Pause the active loop."""

        self.loop.pause()

    def resume(self) -> None:
        """Resume the active loop."""

        self.loop.resume()

    def inject_instruction(self, instruction: str) -> None:
        """Inject a human instruction into the active loop."""

        self.loop.inject_instruction(instruction)

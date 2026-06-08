"""Configuration objects for SuperAgent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Any, Literal


@dataclass(slots=True)
class AgentConfig:
    """Runtime configuration for a SuperAgent instance."""

    agent_id: str = "agent-1"
    workspace_dir: Path = Path.cwd()
    data_dir: Path = Path(".superagent/data")
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    vision_model: str = "gpt-4o-mini"
    action_model: str = "gpt-4o-mini"
    api_base_url: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    desktop_api_url: str = "http://127.0.0.1:7777"
    desktop_host: str = "127.0.0.1"
    desktop_port: int = 8000
    desktop_auth_token: str | None = None
    dry_run: bool = False
    stream_host: str = "127.0.0.1"
    stream_port: int = 6901
    hls_port: int = 7080
    stream_quality: Literal["4k", "1080p"] = "4k"
    enable_hls_fallback: bool = True
    enable_webrtc: bool = True
    enable_ocr: bool = True
    enable_memory: bool = True
    enable_scheduler: bool = True
    enable_monitor: bool = True
    enable_escalation: bool = False
    escalation_webhook_url: str | None = None
    max_steps: int = 40
    stuck_threshold: int = 3
    heartbeat_interval_seconds: int = 30
    default_timeout_seconds: float = 30.0
    state_dir: Path = Path(".superagent/state")
    session_dir: Path = Path(".superagent/sessions")
    memory_db_path: Path = Path(".superagent/memory.sqlite3")
    stream_dir: Path = Path(tempfile.gettempdir()) / "agent-stream"
    log_dir: Path = Path(".superagent/logs")
    totp_secrets: list[str] = field(default_factory=list)
    verification_email: str | None = None
    imap_host: str | None = None
    imap_user: str | None = None
    imap_app_password: str | None = None
    verification_required: bool = False
    human_verification_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolved_workspace_dir(self) -> Path:
        """Return the workspace directory as an absolute path."""

        return self.workspace_dir.expanduser().resolve()

    def resolved_data_dir(self) -> Path:
        """Return the persistent data directory as an absolute path."""

        return self.data_dir.expanduser().resolve()

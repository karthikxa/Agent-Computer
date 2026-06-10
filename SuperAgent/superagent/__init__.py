"""SuperAgent package.

This package exposes the core building blocks for a local computer-use agent
stack: provider abstractions, action models, desktop API helpers, memory,
queueing, scheduling, streaming, and the top-level agent/pool orchestration.

Phase 1 — Streaming/Relay:  relay, pipeline, trajectory
Phase 2 — Security/Perms:   rbac, vault, copilot
Phase 3 — LLM/Browser:      plugin_registry, shell_sim, syscall, app_manager,
                              download_manager, context_manager
Phase 4 — Monitoring:       alert_manager, health_checker, cost_tracker
"""

from .agent import SuperAgent
from .actions import (
    Action,
    ActionExecutor,
    ActionParser,
    ClickAction,
    DragAction,
    KeyAction,
    ScrollAction,
    ShellAction,
    StopAction,
    TextAction,
    WaitAction,
)
from .config import AgentConfig, AdvancedConfig
from .cost_tracker import CostTracker, BudgetExceededError
from .desktop_api import DesktopAPI, DesktopConnectionError
from .grounding import CoordinateGrounding, GroundingModel, OSAtlasGrounding
from .loop import AgentLoop
from .memory import AgentMemory, SQLiteMemory
from .monitor import WatchdogManager
from .ocr import OCRLayer
from .pool import AgentPool
from .providers import (
    BaseProvider,
    AnthropicProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    FireworksProvider,
    HuggingFaceProvider,
    LocalProvider,
    MoonshotProvider,
    MistralProvider,
    OpenAIProvider,
    OpenRouterProvider,
    OllamaProvider,
    OSAtlasProvider,
    QwenProvider,
)
from .queue import PriorityTaskQueue
from .scheduler import TaskScheduler
from .session import SessionManager
from .stream import StreamManager, StreamConfig
from .verification import HumanVerificationHandler

# Advanced Features
from .mcp_server import MCPServer, MCPTool
from .kernel_scheduler import LLMKernel, KernelRequest, get_kernel
from .semantic_fs import SemanticFileSystem, SFSFile
from .hitl import HITLServer
from .virtual_input import VirtualInputDriver
from .security import SecurityManager, SecurityConfig, PermissionProfile
from .benchmark import BenchmarkRunner, BenchmarkTask
from .dashboard_api import (
    DashboardAPIServer,
    register_agent_desktop,
    unregister_agent_desktop,
    get_agent_permissions,
)

# Phase 1 — Streaming / Agent coordination
from .relay import RelayServer
from .pipeline import AgentPipeline
from .trajectory import TrajectoryRecorder, TrajectoryReplayer

# Phase 2 — Security / Permissions
from .rbac import RBACManager, Role
from .vault import CredentialVault, Credential, OAuthToken
from .copilot import CoPilotServer, CoPilotSession

# Phase 3 — LLM / Browser / OS
from .plugin_registry import PluginRegistry, ToolSpec, ToolResult, get_registry
from .shell_sim import AgentShell, ShellResult
from .syscall import SyscallDispatcher, SyscallPolicy, SyscallResult
from .app_manager import AppManager, AppProcess, InstallResult
from .download_manager import DownloadManager, DownloadRecord
from .context_manager import ContextManager

# Phase 4 — Monitoring / Observability
from .alert_manager import AlertManager, Alert, AlertSeverity
from .health_checker import HealthChecker, HealthRecord, AgentHealth

DesktopAPIClient = DesktopAPI

__all__ = [
    # Core
    "Action", "ActionExecutor", "ActionParser",
    "AgentConfig", "AdvancedConfig",
    "AgentMemory", "AgentPool", "AgentLoop",
    "AnthropicProvider", "BaseProvider",
    "ClickAction", "CoordinateGrounding",
    "CostTracker", "BudgetExceededError",
    "DesktopAPI", "DesktopAPIClient", "DesktopConnectionError",
    "DeepSeekProvider", "DragAction",
    "GeminiProvider", "GroundingModel", "GroqProvider",
    "HumanVerificationHandler", "KeyAction",
    "LocalProvider", "FireworksProvider", "HuggingFaceProvider",
    "MistralProvider", "OCRLayer",
    "OpenAIProvider", "OpenRouterProvider", "OllamaProvider",
    "MoonshotProvider", "OSAtlasGrounding", "OSAtlasProvider",
    "PriorityTaskQueue", "QwenProvider",
    "ScrollAction", "SessionManager", "ShellAction",
    "StopAction", "StreamManager", "StreamConfig",
    "SuperAgent", "TaskScheduler", "TextAction",
    "WaitAction", "WatchdogManager", "SQLiteMemory",
    # Advanced
    "MCPServer", "MCPTool",
    "LLMKernel", "KernelRequest", "get_kernel",
    "SemanticFileSystem", "SFSFile",
    "HITLServer", "VirtualInputDriver",
    "SecurityManager", "SecurityConfig", "PermissionProfile",
    "BenchmarkRunner", "BenchmarkTask",
    "DashboardAPIServer", "register_agent_desktop",
    "unregister_agent_desktop", "get_agent_permissions",
    # Phase 1 — Streaming
    "RelayServer", "AgentPipeline",
    "TrajectoryRecorder", "TrajectoryReplayer",
    # Phase 2 — Security
    "RBACManager", "Role",
    "CredentialVault", "Credential", "OAuthToken",
    "CoPilotServer", "CoPilotSession",
    # Phase 3 — LLM/Browser/OS
    "PluginRegistry", "ToolSpec", "ToolResult", "get_registry",
    "AgentShell", "ShellResult",
    "SyscallDispatcher", "SyscallPolicy", "SyscallResult",
    "AppManager", "AppProcess", "InstallResult",
    "DownloadManager", "DownloadRecord",
    "ContextManager",
    # Phase 4 — Monitoring
    "AlertManager", "Alert", "AlertSeverity",
    "HealthChecker", "HealthRecord", "AgentHealth",
]

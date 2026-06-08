"""SuperAgent package.

This package exposes the core building blocks for a local computer-use agent
stack: provider abstractions, action models, desktop API helpers, memory,
queueing, scheduling, streaming, and the top-level agent/pool orchestration.
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
from .cost_tracker import CostTracker, DEFAULT_PRICE_TABLE
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
from .dashboard_api import DashboardAPIServer

DesktopAPIClient = DesktopAPI

__all__ = [
    "Action",
    "ActionExecutor",
    "ActionParser",
    "AgentConfig",
    "AdvancedConfig",
    "AgentMemory",
    "AgentPool",
    "AgentLoop",
    "AnthropicProvider",
    "BaseProvider",
    "ClickAction",
    "CoordinateGrounding",
    "CostTracker",
    "DesktopAPI",
    "DesktopAPIClient",
    "DesktopConnectionError",
    "DeepSeekProvider",
    "DragAction",
    "GeminiProvider",
    "GroundingModel",
    "GroqProvider",
    "HumanVerificationHandler",
    "KeyAction",
    "LocalProvider",
    "FireworksProvider",
    "HuggingFaceProvider",
    "MistralProvider",
    "OCRLayer",
    "OpenAIProvider",
    "OpenRouterProvider",
    "OllamaProvider",
    "MoonshotProvider",
    "OSAtlasGrounding",
    "OSAtlasProvider",
    "PriorityTaskQueue",
    "QwenProvider",
    "ScrollAction",
    "SessionManager",
    "ShellAction",
    "StopAction",
    "StreamManager",
    "StreamConfig",
    "SuperAgent",
    "TaskScheduler",
    "TextAction",
    "WaitAction",
    "WatchdogManager",
    "SQLiteMemory",
    "DEFAULT_PRICE_TABLE",
    "MCPServer",
    "MCPTool",
    "LLMKernel",
    "KernelRequest",
    "get_kernel",
    "SemanticFileSystem",
    "SFSFile",
    "HITLServer",
    "VirtualInputDriver",
    "SecurityManager",
    "SecurityConfig",
    "PermissionProfile",
    "BenchmarkRunner",
    "BenchmarkTask",
    "DashboardAPIServer",
]

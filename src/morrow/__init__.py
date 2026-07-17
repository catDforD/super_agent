"""Public API for the Morrow Python agent."""

from importlib.metadata import PackageNotFoundError, version

from morrow.core import (
    Agent,
    AgentTurn,
    CancellationToken,
    Model,
    ModelEvent,
    ModelRequest,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
    ToolResult,
    ToolRuntime,
)
from morrow.protocol import Message, PermissionProfile, Session
from morrow.runtime.agent import run_agent_turn
from morrow.tools import Tool, ToolRegistry

try:
    __version__ = version("morrow-py")
except PackageNotFoundError:  # pragma: no cover - editable source without metadata
    __version__ = "0.1.0"

__all__ = [
    "Agent",
    "AgentTurn",
    "CancellationToken",
    "Message",
    "Model",
    "ModelEvent",
    "ModelRequest",
    "PermissionProfile",
    "Session",
    "Tool",
    "ToolExecution",
    "ToolExecutionContext",
    "ToolExecutionMode",
    "ToolRegistry",
    "ToolResult",
    "ToolRuntime",
    "__version__",
    "run_agent_turn",
]

"""Built-in and MCP tool runtime for Morrow."""

from .builtins import TOOL_CANCELLED_ERROR, BuiltInTools, built_in_definitions
from .mcp import (
    McpDiscovery,
    McpToolCache,
    McpToolProvider,
    build_tool_name,
    discover_tools,
)
from .patching import (
    ParsedPatchOperation,
    PatchHunk,
    PatchOperationKind,
    StagedPatchChange,
    commit_patch_changes,
    parse_patch,
    plan_patch_changes,
    render_file_diff,
)
from .registry import (
    DuplicateToolError,
    Tool,
    ToolRegistry,
    ToolRegistryBuild,
    ToolRegistryError,
)

__all__ = [
    "BuiltInTools",
    "DuplicateToolError",
    "McpDiscovery",
    "McpToolCache",
    "McpToolProvider",
    "ParsedPatchOperation",
    "PatchHunk",
    "PatchOperationKind",
    "StagedPatchChange",
    "TOOL_CANCELLED_ERROR",
    "Tool",
    "ToolRegistry",
    "ToolRegistryBuild",
    "ToolRegistryError",
    "build_tool_name",
    "built_in_definitions",
    "commit_patch_changes",
    "discover_tools",
    "parse_patch",
    "plan_patch_changes",
    "render_file_diff",
]

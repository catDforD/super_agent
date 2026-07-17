"""Tool interface and registry."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from morrow.core import (
    ToolApproval,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
)
from morrow.protocol import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionProfile,
    ToolCall,
    ToolDefinition,
)
from morrow.sandbox import PermissionEvaluator, PermissionEvaluatorError


class ToolRegistryError(ValueError):
    """Raised when a registry cannot be constructed safely."""


class DuplicateToolError(ToolRegistryError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"duplicate tool registered: {name}")


class Tool(Protocol):
    def definitions(self) -> list[ToolDefinition]: ...

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
        del call
        return ToolExecutionMode.CONCURRENT

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution: ...


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    definition: ToolDefinition
    tool: Tool


@dataclass(frozen=True, slots=True)
class ToolRegistryBuild:
    registry: ToolRegistry
    diagnostics: list[str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: list[_RegisteredTool] = []

    @classmethod
    def empty(cls) -> ToolRegistry:
        return cls()

    @classmethod
    def built_in(cls, root: str | Path, permissions: PermissionProfile) -> ToolRegistry:
        from .builtins import BuiltInTools

        try:
            evaluator = PermissionEvaluator(root, permissions)
        except PermissionEvaluatorError as exc:
            raise ToolRegistryError(str(exc)) from exc
        registry = cls.empty()
        registry.register(BuiltInTools(evaluator))
        return registry

    @classmethod
    async def with_mcp_cache_async(
        cls,
        root: str | Path,
        permissions: PermissionProfile,
        mcp_servers: Iterable[object],
        mcp_cache: Any,
    ) -> ToolRegistryBuild:
        from .mcp import discover_tools

        workspace_root = _resolve_workspace_root(root)
        registry = cls.built_in(workspace_root, permissions)
        discovery = await discover_tools(workspace_root, list(mcp_servers), mcp_cache)
        for tool in discovery.tools:
            registry.register(tool)
        return ToolRegistryBuild(registry, discovery.diagnostics)

    def register(self, tool: Tool) -> None:
        definitions = list(tool.definitions())
        new_names: set[str] = set()
        existing_names = {_definition_name(item.definition) for item in self._tools}
        for definition in definitions:
            name = _definition_name(definition)
            if name in new_names or name in existing_names:
                raise DuplicateToolError(name)
            new_names.add(name)
        self._tools.extend(
            _RegisteredTool(_copy_definition(definition), tool) for definition in definitions
        )

    def definitions(self) -> list[ToolDefinition]:
        return [_copy_definition(registered.definition) for registered in self._tools]

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
        registered = self._find(call.function.name)
        if registered is None:
            return ToolExecutionMode.CONCURRENT
        execution_mode = getattr(registered.tool, "execution_mode", None)
        if execution_mode is None:
            return ToolExecutionMode.CONCURRENT
        return cast(ToolExecutionMode, execution_mode(call))

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution:
        registered = self._find(call.function.name)
        if registered is None:
            return ToolExecution.error(f"unknown tool {call.function.name!r}")
        return await registered.tool.execute(call, approval, context or ToolExecutionContext())

    async def execute_with_context(
        self, call: ToolCall, context: ToolExecutionContext
    ) -> ToolExecution:
        return await self.execute(call, None, context)

    async def execute_approved(
        self,
        call: ToolCall,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> ToolExecution:
        return await self.execute(call, ToolApproval(decision, request))

    async def execute_approved_with_context(
        self,
        call: ToolCall,
        decision: ApprovalDecision,
        request: ApprovalRequest,
        context: ToolExecutionContext,
    ) -> ToolExecution:
        return await self.execute(call, ToolApproval(decision, request), context)

    def _find(self, name: str) -> _RegisteredTool | None:
        return next(
            (
                registered
                for registered in self._tools
                if _definition_name(registered.definition) == name
            ),
            None,
        )

    def __repr__(self) -> str:
        names = [_definition_name(item.definition) for item in self._tools]
        return f"ToolRegistry(tools={names!r})"


def _definition_name(definition: ToolDefinition) -> str:
    return definition.function.name


def _copy_definition(definition: ToolDefinition) -> ToolDefinition:
    copier = getattr(definition, "model_copy", None)
    return copier(deep=True) if copier is not None else definition


def _resolve_workspace_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve(strict=True)


__all__ = [
    "DuplicateToolError",
    "Tool",
    "ToolRegistry",
    "ToolRegistryBuild",
    "ToolRegistryError",
]

"""Application-level orchestration for one durable agent turn."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar, cast

from morrow.config import (
    ContextConfig,
    McpServerConfig,
    ModelContextLimits,
    PlcSubagentConfig,
)
from morrow.core import (
    Agent,
    CancellationToken,
    Model,
    ToolExecutionContext,
    ToolRuntime,
)
from morrow.protocol import (
    AgentEvent,
    AgentEventType,
    ApprovalDecision,
    ApprovalRequest,
    PermissionProfile,
    Session,
    TurnRecord,
)
from morrow.runtime.compaction import maybe_auto_compact_with_tools
from morrow.runtime.events import AgentEventEnvelope, make_event_envelope
from morrow.tools.mcp import McpToolCache
from morrow.tools.plc_subagents import PLC_SUBAGENT_GUIDANCE, PlcSubagentTools
from morrow.tools.registry import ToolRegistry

_Value = TypeVar("_Value")


class RuntimeError(Exception):
    """Base error for runtime composition failures."""


class AgentRunError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(f"agent run failed: {message}")


class EventHandlerError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(f"turn event handler failed: {message}")


class _CooperativeCancellation(Exception):
    """The caller-provided cancellation token fired."""


@dataclass(slots=True)
class RunAgentTurnContext:
    client: Model
    system_prompt: str
    context_config: ContextConfig
    model_limits: ModelContextLimits
    workspace_root: Path
    permissions: PermissionProfile
    mcp_servers: Sequence[McpServerConfig] = field(default_factory=tuple)
    plc_subagents: PlcSubagentConfig = field(default_factory=PlcSubagentConfig)
    mcp_cache: McpToolCache | None = None
    session_name: str = "default"
    turn_index: int = 0
    # Injection seam for tests and alternate tool systems. Production callers normally leave it
    # unset so the built-in/MCP registry is assembled from the fields above.
    tool_runtime: ToolRuntime | None = None


@dataclass(frozen=True, slots=True)
class RunAgentTurnOutcome:
    session_changed: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ToolBuild:
    registry: ToolRuntime
    diagnostics: list[str]


class TurnEventHandler:
    """Observer and approval port used by CLI and server frontends."""

    def on_event(self, event: AgentEventEnvelope) -> None | Awaitable[None]:
        del event
        return None

    async def resolve_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.deny(request.id)


async def run_agent_turn(
    context: RunAgentTurnContext,
    session: Session,
    prompt: str,
    handler: TurnEventHandler,
) -> RunAgentTurnOutcome:
    return await run_agent_turn_with_cancellation(
        context,
        session,
        prompt,
        handler,
        CancellationToken(),
    )


async def run_agent_turn_with_cancellation(
    context: RunAgentTurnContext,
    session: Session,
    prompt: str,
    handler: TurnEventHandler,
    cancellation: CancellationToken,
) -> RunAgentTurnOutcome:
    """Run against a draft and commit it only after orchestration has closed cleanly."""

    owned_cache: McpToolCache | None = None
    effective_context = context
    try:
        if context.tool_runtime is None and context.mcp_cache is None:
            owned_cache = McpToolCache()
            effective_context = replace(context, mcp_cache=owned_cache)
        draft = session.model_copy(deep=True)
        outcome = await _run_agent_turn_inner(
            effective_context,
            draft,
            prompt,
            handler,
            cancellation,
        )
        _replace_session(session, draft)
        return outcome
    finally:
        if owned_cache is not None:
            await owned_cache.aclose()


async def _run_agent_turn_inner(
    context: RunAgentTurnContext,
    session: Session,
    prompt: str,
    handler: TurnEventHandler,
    cancellation: CancellationToken,
) -> RunAgentTurnOutcome:
    if cancellation.is_cancelled:
        return _record_cancelled_turn(session, prompt)

    try:
        build = await _build_tools(context, cancellation)
    except _CooperativeCancellation:
        return _record_cancelled_turn(session, prompt)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(str(error)) from error

    if cancellation.is_cancelled:
        return _record_cancelled_turn(session, prompt)

    tools = build.registry
    definitions = tools.definitions()
    effective_system_prompt = _effective_system_prompt(context.system_prompt, context.plc_subagents)
    try:
        compaction = _await_with_cancellation(
            maybe_auto_compact_with_tools(
                context.client,
                effective_system_prompt,
                session,
                context.context_config,
                context.model_limits,
                prompt,
                definitions,
            ),
            cancellation,
        )
        await compaction
    except _CooperativeCancellation:
        return _record_cancelled_turn(session, prompt)
    except Exception as error:
        message = f"context compaction failed: {error}"
        session.apply_turn(TurnRecord.failed_user_prompt(prompt, message))
        return RunAgentTurnOutcome(session_changed=True, error=message)

    agent = Agent.with_tools(context.client, effective_system_prompt, tools)
    event_index = 0
    for diagnostic in build.diagnostics:
        envelope = make_event_envelope(
            context.session_name,
            context.workspace_root,
            context.turn_index,
            event_index,
            AgentEvent.warning(diagnostic),
        )
        event_index += 1
        try:
            await _deliver_event(handler, envelope)
        except EventHandlerError as error:
            return _record_failed_turn(session, prompt, str(error))

    turn = agent.run_turn_with_context(
        session.active_thread,
        prompt,
        ToolExecutionContext(cancellation=cancellation),
    )
    agent_error: str | None = None
    handler_error: str | None = None
    turn_completed = False
    cancellation_observed = False

    while True:
        if cancellation.is_cancelled and not cancellation_observed:
            turn.cancel()
            cancellation_observed = True
        try:
            event = await anext(turn)
        except StopAsyncIteration:
            break

        envelope = make_event_envelope(
            context.session_name,
            context.workspace_root,
            context.turn_index,
            event_index,
            event,
        )
        event_index += 1
        if event.type is AgentEventType.TURN_COMPLETED:
            turn_completed = True
        elif event.type is AgentEventType.ERROR:
            agent_error = cast(str, event.data)

        if handler_error is None:
            try:
                await _deliver_event(handler, envelope)
            except EventHandlerError as error:
                handler_error = str(error)
                turn.cancel_with_reason(handler_error)
                cancellation_observed = True
                continue

        if event.type is AgentEventType.APPROVAL_REQUESTED:
            request = cast(ApprovalRequest, event.data)
            if cancellation_observed:
                decision = ApprovalDecision.deny(request.id)
            else:
                try:
                    decision = await _await_with_cancellation(
                        handler.resolve_approval(request),
                        cancellation,
                    )
                except _CooperativeCancellation:
                    turn.cancel()
                    cancellation_observed = True
                    continue
                except Exception as error:
                    handler_error = str(EventHandlerError(str(error)))
                    turn.cancel_with_reason(handler_error)
                    cancellation_observed = True
                    continue
            try:
                turn.resolve_approval(decision)
            except Exception as error:
                turn.cancel_with_reason(str(error))
                agent_error = str(error)

    session.apply_turn(turn.into_turn_record())
    final_error = handler_error
    if final_error is None and not turn_completed:
        final_error = agent_error
    return RunAgentTurnOutcome(session_changed=True, error=final_error)


async def _build_tools(
    context: RunAgentTurnContext,
    cancellation: CancellationToken,
) -> _ToolBuild:
    if context.tool_runtime is not None:
        return _ToolBuild(registry=context.tool_runtime, diagnostics=[])
    cache = context.mcp_cache
    if cache is None:  # pragma: no cover - public entrypoints inject an owned cache
        raise RuntimeError("MCP cache is required when building the default tool registry")
    build = ToolRegistry.with_mcp_cache_async(
        context.workspace_root,
        context.permissions,
        context.mcp_servers,
        cache,
    )
    result = await _await_with_cancellation(build, cancellation)
    if context.plc_subagents.enabled:
        result.registry.register(PlcSubagentTools(context.plc_subagents))
    return _ToolBuild(registry=result.registry, diagnostics=result.diagnostics)


def _effective_system_prompt(system_prompt: str, plc_subagents: PlcSubagentConfig) -> str:
    if not plc_subagents.enabled or PLC_SUBAGENT_GUIDANCE in system_prompt:
        return system_prompt
    separator = "\n\n" if system_prompt else ""
    return f"{system_prompt}{separator}{PLC_SUBAGENT_GUIDANCE}"


async def _await_with_cancellation(
    awaitable: Awaitable[_Value],
    cancellation: CancellationToken,
) -> _Value:
    operation = asyncio.ensure_future(awaitable)
    cancelled = asyncio.create_task(cancellation.cancelled())
    try:
        done, _ = await asyncio.wait(
            {operation, cancelled},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done:
            operation.cancel()
            await _consume_task(operation)
            raise _CooperativeCancellation
        return await operation
    except asyncio.CancelledError:
        operation.cancel()
        await _consume_task(operation)
        raise
    finally:
        cancelled.cancel()
        await _consume_task(cancelled)


async def _deliver_event(handler: TurnEventHandler, envelope: AgentEventEnvelope) -> None:
    try:
        result = handler.on_event(envelope)
        if inspect.isawaitable(result):
            await result
    except Exception as error:
        raise EventHandlerError(str(error)) from error


def _record_cancelled_turn(session: Session, prompt: str) -> RunAgentTurnOutcome:
    return _record_failed_turn(session, prompt, "turn cancelled")


def _record_failed_turn(
    session: Session,
    prompt: str,
    message: str,
) -> RunAgentTurnOutcome:
    session.apply_turn(TurnRecord.failed_user_prompt(prompt, message))
    return RunAgentTurnOutcome(session_changed=True, error=message)


def _replace_session(target: Session, source: Session) -> None:
    target.active_thread = source.active_thread.model_copy(deep=True)
    target.turns = [record.model_copy(deep=True) for record in source.turns]
    target.context = source.context.model_copy(deep=True)


async def _consume_task(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


__all__ = [
    "AgentRunError",
    "EventHandlerError",
    "RunAgentTurnContext",
    "RunAgentTurnOutcome",
    "RuntimeError",
    "TurnEventHandler",
    "run_agent_turn",
    "run_agent_turn_with_cancellation",
]

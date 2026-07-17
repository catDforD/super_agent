"""Core agent state machine and provider/tool ports.

This module intentionally contains no HTTP, filesystem, or persistence code.  It owns the
interfaces those adapters implement and the ordering rules for a single agent turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from morrow.protocol import (
    AgentEvent,
    ApprovalDecision,
    ApprovalRequest,
    Conversation,
    Message,
    Thread,
    ToolCall,
    ToolDefinition,
    ToolExecutionSummary,
    Turn,
    TurnRecord,
    TurnStep,
)

DEFAULT_MAX_TOOL_ROUNDS = 8
MAX_CONCURRENT_TOOL_CALLS = 4


@dataclass(slots=True)
class ModelRequest:
    """A complete provider request for one model call."""

    conversation: Conversation
    tools: list[ToolDefinition] = field(default_factory=list)


class ModelEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALLS = "tool_calls"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ModelEvent:
    """Provider-neutral streaming event consumed by :class:`Agent`."""

    type: ModelEventType
    data: str | tuple[ToolCall, ...] | None = None

    @classmethod
    def text_delta(cls, text: str) -> ModelEvent:
        return cls(ModelEventType.TEXT_DELTA, text)

    @classmethod
    def tool_calls(cls, calls: list[ToolCall] | tuple[ToolCall, ...]) -> ModelEvent:
        return cls(ModelEventType.TOOL_CALLS, tuple(calls))

    @classmethod
    def completed(cls) -> ModelEvent:
        return cls(ModelEventType.COMPLETED)

    @property
    def text(self) -> str:
        if self.type is not ModelEventType.TEXT_DELTA or not isinstance(self.data, str):
            raise TypeError("model event is not a text delta")
        return self.data

    @property
    def calls(self) -> list[ToolCall]:
        if self.type is not ModelEventType.TOOL_CALLS or not isinstance(self.data, tuple):
            raise TypeError("model event does not contain tool calls")
        return list(self.data)


class ModelFailure(RuntimeError):
    """A model adapter failed before producing a terminal event."""


@runtime_checkable
class Model(Protocol):
    """Port implemented by streaming model adapters."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class ToolExecutionMode(StrEnum):
    CONCURRENT = "concurrent"
    SERIAL = "serial"


@dataclass(frozen=True, slots=True)
class ToolApproval:
    decision: ApprovalDecision
    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str
    error: str | None = None
    summary: ToolExecutionSummary | None = None

    @classmethod
    def error_result(cls, error: str) -> ToolResult:
        content = json.dumps(
            {"ok": False, "error": error},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return cls(
            ok=False,
            content=content,
            error=error,
            summary=ToolExecutionSummary.error(error),
        )


class ToolExecutionType(StrEnum):
    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    type: ToolExecutionType
    result: ToolResult | None = None
    request: ApprovalRequest | None = None

    def __post_init__(self) -> None:
        if self.type is ToolExecutionType.COMPLETED and self.result is None:
            raise ValueError("completed tool execution requires a result")
        if self.type is ToolExecutionType.APPROVAL_REQUIRED and self.request is None:
            raise ValueError("approval-required tool execution requires a request")

    @classmethod
    def completed(cls, result: ToolResult) -> ToolExecution:
        return cls(ToolExecutionType.COMPLETED, result=result)

    @classmethod
    def approval_required(cls, request: ApprovalRequest) -> ToolExecution:
        return cls(ToolExecutionType.APPROVAL_REQUIRED, request=request)

    @classmethod
    def error(cls, error: str) -> ToolExecution:
        return cls.completed(ToolResult.error_result(error))


class CancellationToken:
    """Cooperative cancellation signal shared by runtime, core, and tools."""

    __slots__ = ("_callbacks", "_event")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._callbacks: list[Callable[[], None]] = []

    def cancel(self) -> None:
        if self._event.is_set():
            return
        self._event.set()
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            _run_cancellation_callback(callback)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def cancelled(self) -> None:
        await self._event.wait()

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a best-effort synchronous cancellation callback.

        The returned function removes the callback when it has not fired yet.
        """

        if self.is_cancelled:
            _run_cancellation_callback(callback)
            return lambda: None
        self._callbacks.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return remove

    def __repr__(self) -> str:
        return f"CancellationToken(cancelled={self.is_cancelled!r})"


@dataclass(slots=True)
class ToolExecutionContext:
    cancellation: CancellationToken = field(default_factory=CancellationToken)


@runtime_checkable
class ToolRuntime(Protocol):
    """Port implemented by the complete tool registry."""

    def definitions(self) -> list[ToolDefinition]: ...

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode: ...

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution: ...


class EmptyToolRuntime:
    def definitions(self) -> list[ToolDefinition]:
        return []

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
        del call
        return ToolExecutionMode.CONCURRENT

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution:
        del approval, context
        return ToolExecution.error(f"unknown tool {call.function.name!r}")


EMPTY_TOOL_RUNTIME = EmptyToolRuntime()


class AgentError(RuntimeError):
    """The agent state machine received an invalid operation."""


class ApprovalError(AgentError):
    """An approval decision did not match the pending request."""


class _CooperativeCancellation(Exception):
    """The turn's explicit cancellation token fired."""


@dataclass(slots=True)
class _PendingApproval:
    index: int
    tool_call: ToolCall
    request: ApprovalRequest


class Agent:
    """Reusable agent configured with one model and one tool registry."""

    def __init__(
        self,
        model: Model,
        system_prompt: str,
        tools: ToolRuntime | None = None,
        *,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> None:
        if max_tool_rounds < 0:
            raise ValueError("max_tool_rounds cannot be negative")
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools or EMPTY_TOOL_RUNTIME
        self.max_tool_rounds = max_tool_rounds

    @classmethod
    def with_tools(
        cls,
        model: Model,
        system_prompt: str,
        tools: ToolRuntime,
        *,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> Agent:
        return cls(model, system_prompt, tools, max_tool_rounds=max_tool_rounds)

    def run_turn(self, thread: Thread, prompt: str) -> AgentTurn:
        return self.run_turn_with_context(thread, prompt, ToolExecutionContext())

    def run_turn_with_context(
        self,
        thread: Thread,
        prompt: str,
        tool_context: ToolExecutionContext,
    ) -> AgentTurn:
        user_message = Message.user(prompt)
        conversation = Conversation.with_system_prompt(self.system_prompt)
        conversation.messages.extend(message.model_copy(deep=True) for message in thread.messages)
        conversation.push(user_message.model_copy(deep=True))
        definitions = [definition.model_copy(deep=True) for definition in self.tools.definitions()]
        return AgentTurn(
            model=self.model,
            tools=self.tools,
            tool_context=tool_context,
            tool_definitions=definitions,
            max_tool_rounds=self.max_tool_rounds,
            conversation=conversation,
            user_message=user_message,
        )

    def __repr__(self) -> str:
        return (
            "Agent("
            f"system_prompt={self.system_prompt!r}, "
            f"tool_count={len(self.tools.definitions())}, "
            f"max_tool_rounds={self.max_tool_rounds})"
        )


class AgentTurn(AsyncIterator[AgentEvent]):
    """An asynchronously driven, inspectable agent turn.

    Iteration pauses after ``approval_requested`` until :meth:`resolve_approval` is called.  The
    caller can always obtain a terminal :class:`TurnRecord`, including after cancellation.
    """

    def __init__(
        self,
        *,
        model: Model,
        tools: ToolRuntime,
        tool_context: ToolExecutionContext,
        tool_definitions: list[ToolDefinition],
        max_tool_rounds: int,
        conversation: Conversation,
        user_message: Message,
    ) -> None:
        self._model = model
        self._tools = tools
        self._tool_context = tool_context
        self._tool_definitions = tool_definitions
        self._max_tool_rounds = max_tool_rounds
        self._conversation = conversation
        self._model_stream: AsyncIterator[ModelEvent] | None = None
        self._need_model_start = True
        self._pending_tool_calls: deque[tuple[int, ToolCall]] = deque()
        self._tool_tasks: dict[asyncio.Task[ToolExecution], tuple[int, ToolCall, bool]] = {}
        self._pending_tool_results: dict[int, tuple[ToolCall, ToolExecution]] = {}
        self._next_tool_result_index = 0
        self._active_serial_tool = False
        self._processing_tool_calls = False
        self._pending_approval: _PendingApproval | None = None
        self._approval_ready = asyncio.Event()
        self._turn = Turn.running(user_message.model_copy(deep=True))
        self._turn_messages: list[Message] = [user_message.model_copy(deep=True)]
        self._assistant_text = ""
        self._pending: deque[AgentEvent] = deque([AgentEvent.turn_started()])
        self._finished = False
        self._tool_rounds = 0
        self._iterating = False

    @property
    def turn(self) -> Turn:
        return self._turn

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def pending_approval(self) -> ApprovalRequest | None:
        if self._pending_approval is None:
            return None
        return self._pending_approval.request

    def __aiter__(self) -> AgentTurn:
        return self

    async def __anext__(self) -> AgentEvent:
        if self._iterating:
            raise RuntimeError("AgentTurn does not support concurrent __anext__ calls")
        self._iterating = True
        try:
            return await self._next_event()
        finally:
            self._iterating = False

    async def _next_event(self) -> AgentEvent:
        while True:
            if self._pending:
                return self._pending.popleft()
            if self._finished:
                raise StopAsyncIteration
            if self._tool_context.cancellation.is_cancelled:
                self.cancel()
                continue

            if self._pending_approval is not None:
                await self._wait_for_approval_or_cancellation()
                continue

            if self._tool_tasks:
                await self._wait_for_tool_completion()
                continue

            if self._processing_tool_calls:
                self._start_ready_tool_calls()
                self._maybe_finish_tool_batch()
                if self._pending or self._tool_tasks or self._need_model_start:
                    continue

            if self._need_model_start:
                await self._start_model_call()
                continue

            if self._model_stream is not None:
                await self._poll_model()
                continue

            self._fail_turn("agent turn has no active model or tool work")

    def into_turn(self) -> Turn:
        if not self._finished:
            self.cancel()
        return self._turn.model_copy(deep=True)

    def into_turn_record(self) -> TurnRecord:
        if not self._finished:
            self.cancel()
        return TurnRecord.new(
            self._turn.model_copy(deep=True),
            [message.model_copy(deep=True) for message in self._turn_messages],
        )

    def cancel(self) -> None:
        self.cancel_with_reason("turn cancelled")

    def cancel_with_reason(self, error: object) -> None:
        if self._finished:
            return
        self._tool_context.cancellation.cancel()
        for task in self._tool_tasks:
            task.cancel()
            task.add_done_callback(_silence_task)
        self._tool_tasks.clear()
        stream, self._model_stream = self._model_stream, None
        if stream is not None:
            _schedule_iterator_close(stream)
        self._need_model_start = False
        self._pending_tool_calls.clear()
        self._pending_tool_results.clear()
        self._pending_approval = None
        self._approval_ready.set()
        self._processing_tool_calls = False
        self._pending.clear()
        self._fail_turn(str(error))

    def resolve_approval(self, decision: ApprovalDecision) -> None:
        pending = self._pending_approval
        if pending is None:
            raise ApprovalError("received approval decision but no approval is pending")
        if decision.request_id != pending.request.id:
            raise ApprovalError(
                f"approval decision {decision.request_id} does not match "
                f"pending approval {pending.request.id}"
            )

        self._pending_approval = None
        self._pending.append(AgentEvent.approval_resolved(decision))
        if decision.approved:
            self._start_approved_tool_call(
                pending.index,
                pending.tool_call,
                decision,
                pending.request,
            )
        else:
            result = ToolResult.error_result("approval denied")
            self._finish_tool_call(pending.tool_call, result)
            self._next_tool_result_index = pending.index + 1
            self._emit_ready_tool_results()
            self._start_ready_tool_calls()
            self._maybe_finish_tool_batch()
        self._approval_ready.set()

    async def aclose(self) -> None:
        self.cancel()
        await asyncio.sleep(0)

    async def _start_model_call(self) -> None:
        self._need_model_start = False
        request = ModelRequest(
            conversation=self._conversation.model_copy(deep=True),
            tools=[definition.model_copy(deep=True) for definition in self._tool_definitions],
        )
        try:
            stream_or_awaitable = self._model.stream(request)
            if inspect.isawaitable(stream_or_awaitable):
                stream_or_awaitable = await self._await_or_cancel(stream_or_awaitable)
            self._model_stream = stream_or_awaitable
        except _CooperativeCancellation:
            self.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_turn(str(error))

    async def _poll_model(self) -> None:
        stream = self._model_stream
        if stream is None:
            return
        try:
            event = await self._await_or_cancel(anext(stream))
        except StopAsyncIteration:
            self._model_stream = None
            await _close_async_iterator(stream)
            self._fail_turn("model stream ended before completion")
            return
        except _CooperativeCancellation:
            self._model_stream = None
            await _close_async_iterator(stream)
            self.cancel()
            return
        except asyncio.CancelledError:
            self._model_stream = None
            await _close_async_iterator(stream)
            raise
        except Exception as error:
            self._model_stream = None
            await _close_async_iterator(stream)
            self._fail_turn(str(error))
            return

        if event.type is ModelEventType.TEXT_DELTA:
            text = event.text
            self._assistant_text += text
            self._pending.append(AgentEvent.text_delta(text))
        elif event.type is ModelEventType.TOOL_CALLS:
            self._model_stream = None
            await _close_async_iterator(stream)
            self._handle_tool_calls(event.calls)
        elif event.type is ModelEventType.COMPLETED:
            self._model_stream = None
            await _close_async_iterator(stream)
            self._complete_turn()
        else:  # pragma: no cover - guarded by ModelEventType
            self._model_stream = None
            self._fail_turn(f"unsupported model event {event.type!r}")

    async def _await_or_cancel(self, awaitable: Awaitable[Any]) -> Any:
        operation = asyncio.ensure_future(awaitable)
        cancellation = asyncio.create_task(self._tool_context.cancellation.cancelled())
        try:
            done, _ = await asyncio.wait(
                {operation, cancellation},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done and operation not in done:
                operation.cancel()
                await _consume_cancelled(operation)
                raise _CooperativeCancellation
            return await operation
        except asyncio.CancelledError:
            operation.cancel()
            await _consume_cancelled(operation)
            raise
        finally:
            cancellation.cancel()
            await _consume_cancelled(cancellation)

    def _complete_turn(self) -> None:
        assistant_message = Message.assistant(self._assistant_text)
        self._turn_messages.append(assistant_message.model_copy(deep=True))
        self._turn.complete(assistant_message.model_copy(deep=True))
        self._pending.append(AgentEvent.agent_message(self._assistant_text))
        self._pending.append(AgentEvent.turn_completed())
        self._finished = True

    def _fail_turn(self, error: str) -> None:
        if self._finished:
            return
        self._turn.fail(error)
        self._pending.append(AgentEvent.error(error))
        self._finished = True

    def _handle_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        if self._tool_rounds >= self._max_tool_rounds:
            self._fail_turn(f"tool call round limit exceeded ({self._max_tool_rounds})")
            return
        if not tool_calls:
            self._fail_turn("model requested tool_calls but did not provide any tool call")
            return

        seen: set[str] = set()
        previous_ids = {
            step.tool_call_id for step in self._turn.steps if step.tool_call_id is not None
        }
        for call in tool_calls:
            if not call.id.strip():
                self._fail_turn("model returned a tool call with an empty id")
                return
            if call.id in seen or call.id in previous_ids:
                self._fail_turn(f"model returned duplicate tool call id {call.id!r}")
                return
            seen.add(call.id)

        if self._turn.steps:
            self._turn.steps[-1].complete()
        self._tool_rounds += 1
        calls = [call.model_copy(deep=True) for call in tool_calls]
        if self._assistant_text:
            assistant_message = Message.assistant_tool_calls_with_content(
                self._assistant_text,
                calls,
            )
        else:
            assistant_message = Message.assistant_tool_calls(calls)
        self._assistant_text = ""
        self._conversation.push(assistant_message.model_copy(deep=True))
        self._turn_messages.append(assistant_message.model_copy(deep=True))
        self._pending_tool_calls = deque(enumerate(calls))
        self._pending_tool_results.clear()
        self._next_tool_result_index = 0
        self._active_serial_tool = False
        self._processing_tool_calls = True
        self._start_ready_tool_calls()

    def _start_ready_tool_calls(self) -> None:
        if (
            not self._processing_tool_calls
            or self._pending_approval is not None
            or self._active_serial_tool
        ):
            return

        while len(self._tool_tasks) < MAX_CONCURRENT_TOOL_CALLS and self._pending_tool_calls:
            _, next_call = self._pending_tool_calls[0]
            serial = self._tools.execution_mode(next_call) is ToolExecutionMode.SERIAL
            if serial and self._tool_tasks:
                return
            index, call = self._pending_tool_calls.popleft()
            self._start_tool_call(index, call, serial)
            if serial:
                return

    def _start_tool_call(self, index: int, call: ToolCall, serial: bool) -> None:
        self._turn.steps.append(TurnStep.running_tool_call(call.function.name, call.id))
        self._pending.append(AgentEvent.tool_call_started(call.id, call.function.name))
        task = asyncio.create_task(
            self._tools.execute(
                call.model_copy(deep=True),
                None,
                self._tool_context,
            )
        )
        self._tool_tasks[task] = (index, call, serial)
        if serial:
            self._active_serial_tool = True

    def _start_approved_tool_call(
        self,
        index: int,
        call: ToolCall,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        task = asyncio.create_task(
            self._tools.execute(
                call.model_copy(deep=True),
                ToolApproval(decision=decision, request=request),
                self._tool_context,
            )
        )
        self._tool_tasks[task] = (index, call, True)
        self._active_serial_tool = True

    async def _wait_for_tool_completion(self) -> None:
        cancellation = asyncio.create_task(self._tool_context.cancellation.cancelled())
        tool_tasks = set(self._tool_tasks)
        try:
            done, _ = await asyncio.wait(
                {*tool_tasks, cancellation},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                self.cancel()
                return

            completed = sorted(
                (task for task in tool_tasks if task in done),
                key=lambda task: self._tool_tasks[task][0],
            )
            for task in completed:
                index, call, serial = self._tool_tasks.pop(task)
                if serial:
                    self._active_serial_tool = False
                try:
                    execution = task.result()
                except asyncio.CancelledError:
                    self.cancel()
                    return
                except Exception as error:
                    execution = ToolExecution.error(str(error))
                self._finish_tool_execution(index, call, execution)
                self._start_ready_tool_calls()
                self._maybe_finish_tool_batch()
        finally:
            cancellation.cancel()
            await _consume_cancelled(cancellation)

    async def _wait_for_approval_or_cancellation(self) -> None:
        if self._approval_ready.is_set():
            self._approval_ready.clear()
            return
        approval = asyncio.create_task(self._approval_ready.wait())
        cancellation = asyncio.create_task(self._tool_context.cancellation.cancelled())
        try:
            done, _ = await asyncio.wait(
                {approval, cancellation},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                self.cancel()
            elif approval in done:
                self._approval_ready.clear()
        finally:
            approval.cancel()
            cancellation.cancel()
            await _consume_cancelled(approval)
            await _consume_cancelled(cancellation)

    def _finish_tool_execution(
        self,
        index: int,
        call: ToolCall,
        execution: ToolExecution,
    ) -> None:
        if execution.type is ToolExecutionType.COMPLETED:
            result = _require_tool_result(execution)
            self._finish_tool_step(call, result)
        self._pending_tool_results[index] = (call, execution)
        self._emit_ready_tool_results()

    def _emit_ready_tool_results(self) -> None:
        while self._pending_approval is None:
            entry = self._pending_tool_results.pop(self._next_tool_result_index, None)
            if entry is None:
                break
            call, execution = entry
            if execution.type is ToolExecutionType.COMPLETED:
                self._finish_tool_call(call, _require_tool_result(execution))
                self._next_tool_result_index += 1
            else:
                request = _require_approval_request(execution)
                self._pending_approval = _PendingApproval(
                    index=self._next_tool_result_index,
                    tool_call=call,
                    request=request,
                )
                self._approval_ready.clear()
                self._pending.append(AgentEvent.approval_requested(request))

    def _finish_tool_call(self, call: ToolCall, result: ToolResult) -> None:
        self._finish_tool_step(call, result)
        message = Message.tool_result(call.id, result.content)
        self._conversation.push(message.model_copy(deep=True))
        self._turn_messages.append(message.model_copy(deep=True))
        self._pending.append(
            AgentEvent.tool_call_finished(
                call.id,
                call.function.name,
                result.ok,
                result.summary,
            )
        )

    def _finish_tool_step(self, call: ToolCall, result: ToolResult) -> None:
        for step in self._turn.steps:
            if step.tool_call_id == call.id:
                if result.ok:
                    step.complete()
                else:
                    step.fail(result.error or "tool call failed")
                return

    def _maybe_finish_tool_batch(self) -> None:
        if (
            self._processing_tool_calls
            and not self._pending_tool_calls
            and not self._tool_tasks
            and not self._pending_tool_results
            and self._pending_approval is None
        ):
            self._processing_tool_calls = False
            self._turn.steps.append(TurnStep.running_model_call())
            self._need_model_start = True

    def __del__(self) -> None:
        if not getattr(self, "_finished", True):
            self._tool_context.cancellation.cancel()
            for task in self._tool_tasks:
                task.cancel()
                task.add_done_callback(_silence_task)


# Backward-friendly name matching the Rust type while keeping the shorter public Python API.
AgentTurnStream = AgentTurn


async def _consume_cancelled(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _close_async_iterator(stream: AsyncIterator[ModelEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if not callable(close):
        return
    with contextlib.suppress(Exception):
        result = close()
        if inspect.isawaitable(result):
            await result


def _schedule_iterator_close(stream: AsyncIterator[ModelEvent]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_close_async_iterator(stream))
    task.add_done_callback(_silence_task)


def _silence_task(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


def _run_cancellation_callback(callback: Callable[[], None]) -> None:
    # Cancellation is best-effort fan-out: one faulty observer must not prevent the signal from
    # reaching the remaining observers or escape from ``cancel()``.
    with contextlib.suppress(Exception):
        callback()


def _require_tool_result(execution: ToolExecution) -> ToolResult:
    if execution.result is None:
        raise AgentError("completed tool execution did not contain a result")
    return execution.result


def _require_approval_request(execution: ToolExecution) -> ApprovalRequest:
    if execution.request is None:
        raise AgentError("approval-required tool execution did not contain a request")
    return execution.request


__all__ = [
    "Agent",
    "AgentError",
    "AgentTurn",
    "AgentTurnStream",
    "ApprovalError",
    "CancellationToken",
    "DEFAULT_MAX_TOOL_ROUNDS",
    "EMPTY_TOOL_RUNTIME",
    "EmptyToolRuntime",
    "MAX_CONCURRENT_TOOL_CALLS",
    "Model",
    "ModelEvent",
    "ModelEventType",
    "ModelFailure",
    "ModelRequest",
    "ToolApproval",
    "ToolExecution",
    "ToolExecutionContext",
    "ToolExecutionMode",
    "ToolExecutionType",
    "ToolResult",
    "ToolRuntime",
]

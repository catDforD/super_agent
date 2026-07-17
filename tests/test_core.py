from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator

from morrow.core import (
    Agent,
    ApprovalError,
    CancellationToken,
    ModelEvent,
    ModelRequest,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
    ToolResult,
)
from morrow.protocol import (
    AgentEvent,
    AgentEventType,
    ApprovalDecision,
    ApprovalRequest,
    Message,
    Thread,
    ToolCall,
    ToolDefinition,
    TurnStatus,
)


class ScriptedModel:
    def __init__(self, scripts: list[list[ModelEvent] | Exception]) -> None:
        self.scripts = deque(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        script = self.scripts.popleft()

        async def generate() -> AsyncIterator[ModelEvent]:
            if isinstance(script, Exception):
                raise script
            for event in script:
                yield event

        return generate()


class RecordingTools:
    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self.delays = delays or {}
        self.calls: list[str] = []

    def definitions(self) -> list[ToolDefinition]:
        return [ToolDefinition.function("read", "read", {"type": "object"})]

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
        del call
        return ToolExecutionMode.CONCURRENT

    async def execute(
        self,
        call: ToolCall,
        approval: object = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution:
        del approval, context
        self.calls.append(call.id)
        await asyncio.sleep(self.delays.get(call.id, 0))
        return ToolExecution.completed(ToolResult(ok=True, content=json.dumps({"id": call.id})))


def _collect(turn: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    async def run() -> list[AgentEvent]:
        return [event async for event in turn]

    return asyncio.run(run())


def test_turn_streams_text_and_builds_terminal_record() -> None:
    model = ScriptedModel(
        [[ModelEvent.text_delta("Hel"), ModelEvent.text_delta("lo"), ModelEvent.completed()]]
    )
    thread = Thread(messages=[Message.user("earlier")])

    turn = Agent(model, "system").run_turn(thread, "now")
    events = _collect(turn)
    record = turn.into_turn_record()

    assert [event.type for event in events] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.TEXT_DELTA,
        AgentEventType.TEXT_DELTA,
        AgentEventType.AGENT_MESSAGE,
        AgentEventType.TURN_COMPLETED,
    ]
    assert record.turn.status is TurnStatus.COMPLETED
    assert record.turn.assistant_message == Message.assistant("Hello")
    assert [message.content for message in record.messages] == ["now", "Hello"]
    assert [message.content for message in model.requests[0].conversation.messages] == [
        "system",
        "earlier",
        "now",
    ]
    assert thread.messages == [Message.user("earlier")]


def test_terminal_model_event_closes_provider_stream() -> None:
    class ClosingModel:
        def __init__(self) -> None:
            self.closed = False

        def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            del request

            async def generate() -> AsyncIterator[ModelEvent]:
                try:
                    yield ModelEvent.text_delta("done")
                    yield ModelEvent.completed()
                finally:
                    self.closed = True

            return generate()

    model = ClosingModel()

    _collect(Agent(model, "system").run_turn(Thread.new(), "run"))

    assert model.closed is True


def test_concurrent_tool_results_return_to_model_in_original_order() -> None:
    calls = [
        ToolCall.function("slow", "read", "{}"),
        ToolCall.function("fast", "read", "{}"),
    ]
    model = ScriptedModel(
        [
            [ModelEvent.tool_calls(calls)],
            [ModelEvent.text_delta("done"), ModelEvent.completed()],
        ]
    )
    tools = RecordingTools({"slow": 0.03, "fast": 0.001})

    turn = Agent.with_tools(model, "system", tools).run_turn(Thread.new(), "run")
    events = _collect(turn)

    finished_ids = [
        event.data.id for event in events if event.type is AgentEventType.TOOL_CALL_FINISHED
    ]
    result_ids = [
        message.tool_call_id
        for message in model.requests[1].conversation.messages
        if message.tool_call_id is not None
    ]
    assert finished_ids == ["slow", "fast"]
    assert result_ids == ["slow", "fast"]


def test_serial_tool_is_a_barrier_between_concurrent_batches() -> None:
    log: list[str] = []

    class BarrierTools(RecordingTools):
        def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
            if call.function.name == "write":
                return ToolExecutionMode.SERIAL
            return ToolExecutionMode.CONCURRENT

        async def execute(
            self,
            call: ToolCall,
            approval: object = None,
            context: ToolExecutionContext | None = None,
        ) -> ToolExecution:
            del approval, context
            log.append(f"start:{call.id}")
            await asyncio.sleep(0.001)
            log.append(f"end:{call.id}")
            return ToolExecution.completed(ToolResult(ok=True, content="{}"))

    model = ScriptedModel(
        [
            [
                ModelEvent.tool_calls(
                    [
                        ToolCall.function("read-1", "read", "{}"),
                        ToolCall.function("write", "write", "{}"),
                        ToolCall.function("read-2", "read", "{}"),
                    ]
                )
            ],
            [ModelEvent.text_delta("done"), ModelEvent.completed()],
        ]
    )

    _collect(Agent.with_tools(model, "system", BarrierTools()).run_turn(Thread.new(), "run"))

    assert log == [
        "start:read-1",
        "end:read-1",
        "start:write",
        "end:write",
        "start:read-2",
        "end:read-2",
    ]


def test_approval_mismatch_keeps_request_pending_then_denial_resumes() -> None:
    request = ApprovalRequest.shell_command("approval-1", "pwd", ".", 5, "shell")

    class ApprovalTools(RecordingTools):
        def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
            del call
            return ToolExecutionMode.SERIAL

        async def execute(
            self,
            call: ToolCall,
            approval: object = None,
            context: ToolExecutionContext | None = None,
        ) -> ToolExecution:
            del call, context
            if approval is None:
                return ToolExecution.approval_required(request)
            return ToolExecution.completed(ToolResult(ok=True, content="{}"))

    model = ScriptedModel(
        [
            [ModelEvent.tool_calls([ToolCall.function("call-1", "shell", "{}")])],
            [ModelEvent.text_delta("handled"), ModelEvent.completed()],
        ]
    )

    async def run() -> tuple[list[AgentEvent], TurnStatus]:
        turn = Agent.with_tools(model, "system", ApprovalTools()).run_turn(Thread.new(), "run")
        events: list[AgentEvent] = []
        async for event in turn:
            events.append(event)
            if event.type is AgentEventType.APPROVAL_REQUESTED:
                try:
                    turn.resolve_approval(ApprovalDecision.deny("wrong"))
                except ApprovalError:
                    pass
                else:  # pragma: no cover - assertion with clearer failure
                    raise AssertionError("mismatched approval must fail")
                assert turn.pending_approval == request
                turn.resolve_approval(ApprovalDecision.deny(request.id))
        return events, turn.turn.status

    events, status = asyncio.run(run())

    assert status is TurnStatus.COMPLETED
    assert [event.type for event in events if "approval" in event.type.value] == [
        AgentEventType.APPROVAL_REQUESTED,
        AgentEventType.APPROVAL_RESOLVED,
    ]
    tool_result = next(
        message
        for message in model.requests[1].conversation.messages
        if message.tool_call_id == "call-1"
    )
    assert json.loads(tool_result.content or "{}") == {
        "ok": False,
        "error": "approval denied",
    }


def test_duplicate_tool_ids_fail_before_any_tool_starts() -> None:
    calls = [
        ToolCall.function("duplicate", "read", "{}"),
        ToolCall.function("duplicate", "read", "{}"),
    ]
    tools = RecordingTools()
    turn = Agent.with_tools(
        ScriptedModel([[ModelEvent.tool_calls(calls)]]),
        "system",
        tools,
    ).run_turn(Thread.new(), "run")

    events = _collect(turn)

    assert tools.calls == []
    assert events[-1].type is AgentEventType.ERROR
    assert "duplicate tool call id" in events[-1].data
    assert turn.turn.status is TurnStatus.FAILED


def test_tool_round_limit_fails_after_configured_number_of_rounds() -> None:
    model = ScriptedModel(
        [
            [ModelEvent.tool_calls([ToolCall.function("one", "read", "{}")])],
            [ModelEvent.tool_calls([ToolCall.function("two", "read", "{}")])],
        ]
    )
    tools = RecordingTools()
    turn = Agent(model, "system", tools, max_tool_rounds=1).run_turn(Thread.new(), "run")

    events = _collect(turn)

    assert tools.calls == ["one"]
    assert events[-1].type is AgentEventType.ERROR
    assert events[-1].data == "tool call round limit exceeded (1)"
    assert turn.turn.status is TurnStatus.FAILED


def test_cancellation_token_wakes_all_waiters() -> None:
    async def run() -> list[bool]:
        token = CancellationToken()
        waiters = [asyncio.create_task(token.cancelled()) for _ in range(3)]
        await asyncio.sleep(0)
        token.cancel()
        await asyncio.gather(*waiters)
        return [waiter.done() for waiter in waiters]

    assert asyncio.run(run()) == [True, True, True]


def test_cancellation_callback_failure_does_not_block_other_callbacks() -> None:
    token = CancellationToken()
    called: list[str] = []

    def fail() -> None:
        called.append("failed")
        raise ValueError("callback failed")

    token.add_callback(fail)
    token.add_callback(lambda: called.append("completed"))

    token.cancel()
    token.add_callback(fail)

    assert token.is_cancelled is True
    assert called == ["failed", "completed", "failed"]

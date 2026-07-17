from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest

from morrow.config import ContextConfig, ModelContextLimits
from morrow.core import (
    EMPTY_TOOL_RUNTIME,
    CancellationToken,
    ModelEvent,
    ModelRequest,
)
from morrow.protocol import (
    AgentEventType,
    Message,
    PermissionProfile,
    Session,
    ToolDefinition,
    Turn,
    TurnRecord,
    TurnStatus,
)
from morrow.runtime.agent import (
    RunAgentTurnContext,
    TurnEventHandler,
    run_agent_turn,
    run_agent_turn_with_cancellation,
)
from morrow.runtime.compaction import (
    REQUIRED_SUMMARY_SECTIONS,
    CompactionOutcome,
    compact_session,
    estimate_context_tokens,
)
from morrow.runtime.events import EVENT_SCHEMA_VERSION, AgentEventEnvelope
from morrow.tools.registry import ToolRegistry


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


class RecordingHandler(TurnEventHandler):
    def __init__(self, fail_on: AgentEventType | None = None) -> None:
        self.events: list[AgentEventEnvelope] = []
        self.fail_on = fail_on

    def on_event(self, event: AgentEventEnvelope) -> None:
        self.events.append(event)
        if event.event.type is self.fail_on:
            raise ValueError("event sink failed")


def _context(model: ScriptedModel) -> RunAgentTurnContext:
    return RunAgentTurnContext(
        client=model,
        system_prompt="system",
        context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
        model_limits=ModelContextLimits(16_000, 1_000),
        workspace_root=Path("/workspace"),
        permissions=PermissionProfile(),
        session_name="work",
        turn_index=3,
        tool_runtime=EMPTY_TOOL_RUNTIME,
    )


def _completed_record(user: str, assistant: str) -> TurnRecord:
    user_message = Message.user(user)
    assistant_message = Message.assistant(assistant)
    turn = Turn.running(user_message.model_copy(deep=True))
    turn.complete(assistant_message.model_copy(deep=True))
    return TurnRecord.new(turn, [user_message, assistant_message])


def _valid_summary(progress: str = "done") -> str:
    sections = "\n".join(f"{section}\n- {progress}" for section in REQUIRED_SUMMARY_SECTIONS)
    return f"<analysis>compact</analysis>\n<summary>\n{sections}\n</summary>"


def test_runtime_commits_completed_turn_and_stable_event_indices() -> None:
    model = ScriptedModel([[ModelEvent.text_delta("ok"), ModelEvent.completed()]])
    session = Session.new()
    handler = RecordingHandler()

    outcome = asyncio.run(run_agent_turn(_context(model), session, "hello", handler))

    assert outcome.session_changed is True
    assert outcome.error is None
    assert session.turns[0].turn.status is TurnStatus.COMPLETED
    assert [message.content for message in session.active_thread.messages] == ["hello", "ok"]
    assert [envelope.event_index for envelope in handler.events] == [0, 1, 2, 3]
    assert all(envelope.schema_version == EVENT_SCHEMA_VERSION for envelope in handler.events)
    assert all(envelope.session == "work" for envelope in handler.events)
    assert all(envelope.turn_index == 3 for envelope in handler.events)


def test_event_handler_failure_after_completion_keeps_completed_domain_record() -> None:
    model = ScriptedModel([[ModelEvent.text_delta("ok"), ModelEvent.completed()]])
    session = Session.new()

    outcome = asyncio.run(
        run_agent_turn(
            _context(model),
            session,
            "hello",
            RecordingHandler(AgentEventType.TURN_COMPLETED),
        )
    )

    assert outcome.error == "turn event handler failed: event sink failed"
    assert session.turns[0].turn.status is TurnStatus.COMPLETED
    assert [message.content for message in session.active_thread.messages] == ["hello", "ok"]


def test_event_handler_failure_mid_turn_records_failure_without_active_context() -> None:
    model = ScriptedModel([[ModelEvent.text_delta("partial"), ModelEvent.completed()]])
    session = Session.new()

    outcome = asyncio.run(
        run_agent_turn(
            _context(model),
            session,
            "hello",
            RecordingHandler(AgentEventType.TEXT_DELTA),
        )
    )

    assert outcome.error == "turn event handler failed: event sink failed"
    assert session.turns[0].turn.status is TurnStatus.FAILED
    assert session.active_thread.messages == []
    assert session.turns[0].messages == [Message.user("hello")]


def test_cancellation_records_failed_turn_and_preserves_active_context() -> None:
    class WaitingModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])

        def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)

            async def generate() -> AsyncIterator[ModelEvent]:
                await asyncio.Event().wait()
                yield ModelEvent.completed()

            return generate()

    async def run() -> tuple[Session, str | None]:
        model = WaitingModel()
        session = Session.new()
        session.active_thread.push(Message.user("existing"))
        token = CancellationToken()
        task = asyncio.create_task(
            run_agent_turn_with_cancellation(
                _context(model),
                session,
                "new prompt",
                RecordingHandler(),
                token,
            )
        )
        await asyncio.sleep(0)
        token.cancel()
        outcome = await asyncio.wait_for(task, timeout=1)
        return session, outcome.error

    session, error = asyncio.run(run())

    assert error == "turn cancelled"
    assert session.turns[0].turn.status is TurnStatus.FAILED
    assert session.active_thread.messages == [Message.user("existing")]


def test_compaction_retries_invalid_contract_and_rebuilds_active_thread() -> None:
    invalid = "<analysis>x</analysis><summary>missing headings</summary>"
    model = ScriptedModel(
        [
            [ModelEvent.text_delta(invalid), ModelEvent.completed()],
            [ModelEvent.text_delta(_valid_summary("current")), ModelEvent.completed()],
        ]
    )
    session = Session.new()
    for index in range(4):
        session.apply_turn(_completed_record(f"user-{index}", f"assistant-{index}"))

    outcome = asyncio.run(compact_session(model, session, ContextConfig(True, 0.8, 2, 1_000, 2)))

    assert outcome is CompactionOutcome.CHANGED
    assert session.context.summarized_turns == 2
    assert session.context.summary is not None
    assert session.active_thread.messages[0].content.startswith("Session summary:\n")
    assert [message.content for message in session.active_thread.messages[1:]] == [
        "user-2",
        "assistant-2",
        "user-3",
        "assistant-3",
    ]
    repair_prompt = model.requests[1].conversation.messages[-1].content or ""
    assert "Repair feedback" in repair_prompt
    assert "missing required section" in repair_prompt


def test_compaction_model_failure_uses_deterministic_fallback() -> None:
    model = ScriptedModel([RuntimeError("provider down")])
    session = Session.new()
    for index in range(3):
        session.apply_turn(_completed_record(f"user-{index}", f"assistant-{index}"))

    outcome = asyncio.run(compact_session(model, session, ContextConfig(True, 0.8, 1, 1_000, 2)))

    assert outcome is CompactionOutcome.CHANGED
    assert session.context.summary is not None
    assert "deterministic fallback" in session.context.summary
    assert all(section in session.context.summary for section in REQUIRED_SUMMARY_SECTIONS)


def test_context_estimate_accounts_for_tool_definitions() -> None:
    session = Session.new()
    without_tools = estimate_context_tokens("system", session, "prompt")
    with_tools = estimate_context_tokens(
        "system",
        session,
        "prompt",
        [
            ToolDefinition.function(
                "read_file",
                "Read a UTF-8 text file from the workspace",
                {"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
    )

    assert with_tools > without_tools


def test_runtime_closes_only_an_implicitly_created_mcp_cache(tmp_path: Path) -> None:
    class TrackingCache:
        instances: list[TrackingCache] = []

        def __init__(self) -> None:
            self.closed = False
            self.instances.append(self)

        async def aclose(self) -> None:
            self.closed = True

    def context(model: ScriptedModel, cache: TrackingCache | None = None) -> RunAgentTurnContext:
        return RunAgentTurnContext(
            client=model,
            system_prompt="system",
            context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
            model_limits=ModelContextLimits(16_000, 1_000),
            workspace_root=tmp_path,
            permissions=PermissionProfile(),
            mcp_cache=cache,  # type: ignore[arg-type]
        )

    implicit_model = ScriptedModel([[ModelEvent.text_delta("ok"), ModelEvent.completed()]])
    with patch("morrow.runtime.agent.McpToolCache", TrackingCache):
        implicit = asyncio.run(
            run_agent_turn(context(implicit_model), Session.new(), "hello", RecordingHandler())
        )
    assert implicit.error is None
    assert len(TrackingCache.instances) == 1
    assert TrackingCache.instances[0].closed is True

    injected_cache = TrackingCache()
    injected_model = ScriptedModel([[ModelEvent.text_delta("ok"), ModelEvent.completed()]])
    injected = asyncio.run(
        run_agent_turn(
            context(injected_model, injected_cache),
            Session.new(),
            "hello",
            RecordingHandler(),
        )
    )
    assert injected.error is None
    assert injected_cache.closed is False


def test_external_task_cancellation_propagates_without_committing_session() -> None:
    class WaitingModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])
            self.started = asyncio.Event()

        def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(request)

            async def generate() -> AsyncIterator[ModelEvent]:
                self.started.set()
                await asyncio.Event().wait()
                yield ModelEvent.completed()

            return generate()

    async def run() -> Session:
        model = WaitingModel()
        session = Session.new()
        task = asyncio.create_task(
            run_agent_turn(_context(model), session, "hello", RecordingHandler())
        )
        await model.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return session

    session = asyncio.run(run())

    assert session.turns == []
    assert session.active_thread.messages == []


def test_external_cancellation_during_tool_build_propagates_and_closes_owned_cache(
    tmp_path: Path,
) -> None:
    class TrackingCache:
        instance: TrackingCache | None = None

        def __init__(self) -> None:
            self.closed = False
            type(self).instance = self

        async def aclose(self) -> None:
            self.closed = True

    async def run() -> Session:
        started = asyncio.Event()

        async def blocking_build(
            cls: type[ToolRegistry],
            root: str | Path,
            permissions: PermissionProfile,
            mcp_servers: object,
            mcp_cache: object,
        ) -> object:
            del cls, root, permissions, mcp_servers, mcp_cache
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        model = ScriptedModel([[ModelEvent.completed()]])
        session = Session.new()
        context = RunAgentTurnContext(
            client=model,
            system_prompt="system",
            context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
            model_limits=ModelContextLimits(16_000, 1_000),
            workspace_root=tmp_path,
            permissions=PermissionProfile(),
        )
        with (
            patch("morrow.runtime.agent.McpToolCache", TrackingCache),
            patch.object(ToolRegistry, "with_mcp_cache_async", new=classmethod(blocking_build)),
        ):
            task = asyncio.create_task(
                run_agent_turn(context, session, "hello", RecordingHandler())
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return session

    session = asyncio.run(run())

    assert session.turns == []
    assert TrackingCache.instance is not None
    assert TrackingCache.instance.closed is True

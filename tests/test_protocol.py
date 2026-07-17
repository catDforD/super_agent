from __future__ import annotations

import json
from pathlib import Path

import pytest

from morrow.protocol import (
    AgentEvent,
    ApprovalDecision,
    ApprovalRequest,
    FileChangeOperation,
    FileChangeSummary,
    Message,
    PermissionMode,
    PermissionProfile,
    Session,
    SessionApplyError,
    ShellCommandSummary,
    ShellPolicy,
    Thread,
    ToolCall,
    ToolDefinition,
    ToolExecutionSummary,
    Turn,
    TurnRecord,
    TurnStatus,
    TurnStep,
)

GOLDEN_FIXTURES = Path(__file__).parent / "fixtures" / "golden"


def test_openai_message_and_tool_wire_shapes() -> None:
    call = ToolCall.function("call_1", "read_file", '{"path":"pyproject.toml"}')
    definition = ToolDefinition.function(
        "read_file",
        "Read a file",
        {"type": "object", "properties": {"path": {"type": "string"}}},
    )

    assert definition.to_wire()["type"] == "function"
    assert definition.function.name == "read_file"
    assert call.to_wire() == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"pyproject.toml"}'},
    }
    assert Message.assistant_tool_calls([call]).to_wire() == {
        "role": "assistant",
        "content": None,
        "tool_calls": [call.to_wire()],
    }
    assert Message.tool_result("call_1", '{"ok":true}').to_wire() == {
        "role": "tool",
        "content": '{"ok":true}',
        "tool_call_id": "call_1",
    }


def test_turn_and_session_terminal_invariants() -> None:
    session = Session.from_thread(
        Thread(messages=[Message.user("Previous"), Message.assistant("Context")])
    )
    user = Message.user("Hello")
    assistant = Message.assistant("Hi")
    turn = Turn.running(user)
    turn.steps.extend(
        [
            TurnStep.running_tool_call("read_file", "call_1"),
            TurnStep.running_tool_call("list_files", "call_2"),
        ]
    )
    turn.complete(assistant)
    record = TurnRecord.new(turn, [user, assistant])

    session.apply_turn(record)

    assert session.active_thread.messages[-2:] == [user, assistant]
    assert session.turns == [record]
    failed = TurnRecord.failed_user_prompt("Broken", "model error")
    before = session.active_thread.model_copy(deep=True)
    session.apply_turn(failed)
    assert session.active_thread == before
    assert session.turns[-1].turn.status is TurnStatus.FAILED

    running = TurnRecord.new(Turn.running(Message.user("Wait")), [Message.user("Wait")])
    with pytest.raises(SessionApplyError, match="running turn"):
        session.try_apply_turn(running)


def test_failing_turn_closes_every_running_step() -> None:
    turn = Turn.running(Message.user("Hello"))
    turn.steps.append(TurnStep.running_tool_call("read_file", "call_1"))

    turn.fail("turn cancelled")

    assert all(step.status is TurnStatus.FAILED for step in turn.steps)
    assert all(step.error == "turn cancelled" for step in turn.steps)


def test_permission_defaults_and_approval_event_wire_shapes() -> None:
    assert PermissionProfile() == PermissionProfile(
        mode=PermissionMode.READ_ONLY,
        shell=ShellPolicy.PROMPT,
    )
    assert PermissionProfile.for_mode(PermissionMode.DANGER_FULL_ACCESS).shell is ShellPolicy.ALLOW

    request = ApprovalRequest.shell_command(
        "approval-call_1",
        "pytest",
        "/repo",
        30,
        "shell command requires approval",
    )
    decision = ApprovalDecision.approve(request.id)

    assert AgentEvent.approval_requested(request).to_wire() == {
        "type": "approval_requested",
        "data": {
            "id": "approval-call_1",
            "action": {
                "kind": "shell_command",
                "command": "pytest",
                "cwd": "/repo",
                "timeout_secs": 30,
            },
            "reason": "shell command requires approval",
        },
    }
    assert AgentEvent.approval_resolved(decision).to_wire() == {
        "type": "approval_resolved",
        "data": {"request_id": "approval-call_1", "approved": True},
    }
    assert AgentEvent.turn_started().to_wire() == {"type": "turn_started"}


def test_file_approval_and_tool_summaries_omit_empty_fields() -> None:
    file = FileChangeSummary(
        path="src/lib.py",
        operation=FileChangeOperation.UPDATE,
        replacements=2,
        created=False,
        overwritten=True,
        deleted=False,
    )
    file_summary = ToolExecutionSummary.file_changes([file], "--- old\n+++ new\n")
    shell = ShellCommandSummary(
        command="pytest",
        exit_code=None,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    shell_summary = ToolExecutionSummary.shell(shell)
    error_summary = ToolExecutionSummary.error("approval denied")

    assert file_summary.to_wire() == {
        "files": [file.to_wire()],
        "diff": "--- old\n+++ new\n",
    }
    assert shell_summary.shell == shell
    assert shell_summary.to_wire() == {"shell": shell.to_wire()}
    assert error_summary.error == "approval denied"
    assert error_summary.to_wire() == {"error": "approval denied"}
    assert AgentEvent.tool_call_finished("call_1", "read_file", True).to_wire() == {
        "type": "tool_call_finished",
        "data": {"id": "call_1", "name": "read_file", "ok": True},
    }


def test_agent_event_parses_structured_variant_data() -> None:
    event = AgentEvent.model_validate(
        {
            "type": "tool_call_finished",
            "data": {
                "id": "call_1",
                "name": "shell_command",
                "ok": False,
                "summary": {"error": "denied"},
            },
        }
    )

    assert event.to_wire()["data"]["summary"] == {"error": "denied"}


def test_remaining_agent_event_and_approval_variants_match_rust_goldens() -> None:
    file = FileChangeSummary(
        path="src/lib.py",
        operation=FileChangeOperation.UPDATE,
        replacements=2,
        created=False,
        overwritten=True,
        deleted=False,
    )
    diff = "--- src/lib.py\n+++ src/lib.py\n@@\n-old\n+new\n"
    request = ApprovalRequest.file_changes(
        "approval-call_1",
        [file],
        diff,
        "file changes require approval",
    )
    events = {
        "warning": AgentEvent.warning("mcp server docs: failed to start"),
        "text_delta": AgentEvent.text_delta("Hel"),
        "agent_message": AgentEvent.agent_message("Hello"),
        "tool_call_started": AgentEvent.tool_call_started("call_1", "apply_patch"),
        "approval_requested_file_changes": AgentEvent.approval_requested(request),
        "approval_resolved_denied": AgentEvent.approval_resolved(ApprovalDecision.deny(request.id)),
        "tool_call_finished_with_summary": AgentEvent.tool_call_finished(
            "call_1",
            "apply_patch",
            True,
            ToolExecutionSummary.file_changes([file], diff),
        ),
        "turn_completed": AgentEvent.turn_completed(),
        "error": AgentEvent.error("model error"),
    }
    fixture = json.loads(
        (GOLDEN_FIXTURES / "agent_event_variants.json").read_text(encoding="utf-8")
    )

    assert {name: event.to_wire() for name, event in events.items()} == fixture
    assert {
        name: AgentEvent.model_validate(wire).to_wire() for name, wire in fixture.items()
    } == fixture

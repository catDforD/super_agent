from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from morrow.core import (
    CancellationToken,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
    ToolExecutionType,
)
from morrow.protocol import (
    ApprovalDecision,
    PermissionMode,
    PermissionProfile,
    ToolCall,
)
from morrow.tools import ToolRegistry, parse_patch, patching

T = TypeVar("T")


def run(coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def call(name: str, arguments: dict[str, Any], identifier: str = "call_1") -> ToolCall:
    return ToolCall.function(identifier, name, json.dumps(arguments))


def content(execution: ToolExecution) -> dict[str, Any]:
    assert execution.type is ToolExecutionType.COMPLETED
    assert execution.result is not None
    return json.loads(execution.result.content)


def approve(registry: ToolRegistry, tool_call: ToolCall) -> ToolExecution:
    pending = run(registry.execute(tool_call))
    assert pending.type is ToolExecutionType.APPROVAL_REQUIRED
    assert pending.request is not None
    return run(
        registry.execute_approved(
            tool_call,
            ApprovalDecision.approve(pending.request.id),
            pending.request,
        )
    )


def workspace_registry(root: Path) -> ToolRegistry:
    return ToolRegistry.built_in(root, PermissionProfile.for_mode(PermissionMode.WORKSPACE_WRITE))


def test_registry_definitions_modes_and_read_tools(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("One\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hidden").write_text("two", encoding="utf-8")
    registry = workspace_registry(tmp_path)

    names = [definition.function.name for definition in registry.definitions()]
    assert names == [
        "read_file",
        "list_files",
        "search_text",
        "edit_file",
        "write_file",
        "apply_patch",
        "shell_command",
    ]
    assert registry.execution_mode(call("read_file", {"path": "note.txt"})) is (
        ToolExecutionMode.CONCURRENT
    )
    assert registry.execution_mode(call("write_file", {"path": "x", "content": "x"})) is (
        ToolExecutionMode.SERIAL
    )

    read = content(
        run(
            registry.execute(
                call("read_file", {"path": "note.txt", "start_line": 2, "max_lines": 1})
            )
        )
    )
    assert read["data"]["content"] == "two"
    assert read["data"]["truncated"] is True

    listed = content(run(registry.execute(call("list_files", {"recursive": True}))))
    assert [entry["path"] for entry in listed["data"]["entries"]] == ["note.txt"]

    searched = content(run(registry.execute(call("search_text", {"query": "TWO", "path": "."}))))
    assert searched["data"]["results"][0]["path"] == "note.txt"
    assert searched["data"]["results"][0]["line"] == 2


@pytest.mark.anyio
async def test_concurrent_read_tools_run_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "first.txt").write_text("first\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second\n", encoding="utf-8")
    registry = workspace_registry(tmp_path)
    tools = registry._tools[0].tool  # type: ignore[attr-defined]
    original_read = tools._read_file  # type: ignore[attr-defined]
    workers_ready = threading.Barrier(2)

    def synchronized_read(tool_call: ToolCall) -> dict[str, Any]:
        workers_ready.wait(timeout=1)
        return original_read(tool_call)

    monkeypatch.setattr(tools, "_read_file", synchronized_read)
    executions = await asyncio.wait_for(
        asyncio.gather(
            registry.execute(call("read_file", {"path": "first.txt"}, "call_1")),
            registry.execute(call("read_file", {"path": "second.txt"}, "call_2")),
        ),
        timeout=2,
    )

    assert [content(execution)["data"]["content"] for execution in executions] == [
        "first",
        "second",
    ]


def test_file_tools_require_approval_and_reject_drift(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    registry = workspace_registry(tmp_path)
    edit = call(
        "edit_file",
        {"path": "note.txt", "old_text": "old", "new_text": "new"},
    )

    pending = run(registry.execute(edit))
    assert pending.type is ToolExecutionType.APPROVAL_REQUIRED
    assert pending.request is not None
    assert target.read_text(encoding="utf-8") == "old\n"
    assert pending.request.action.diff is not None
    assert "-old" in pending.request.action.diff

    target.write_text("old\nexternal\n", encoding="utf-8")
    rejected = run(
        registry.execute_approved(
            edit,
            ApprovalDecision.approve(pending.request.id),
            pending.request,
        )
    )
    assert content(rejected)["ok"] is False
    assert "approval no longer matches" in content(rejected)["error"]
    assert target.read_text(encoding="utf-8") == "old\nexternal\n"


def test_write_and_apply_patch_commit_atomically_after_approval(tmp_path: Path) -> None:
    (tmp_path / "update.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("gone\n", encoding="utf-8")
    registry = workspace_registry(tmp_path)

    created = approve(registry, call("write_file", {"path": "created.txt", "content": "hello"}))
    assert content(created)["data"]["created"] is True

    patch = """*** Begin Patch
*** Add File: added.txt
+added
*** Update File: update.txt
@@
-before
+after
*** Delete File: delete.txt
*** End Patch
"""
    result = approve(registry, call("apply_patch", {"patch": patch}))
    assert content(result)["data"]["changed_files"] == 3
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert (tmp_path / "update.txt").read_text(encoding="utf-8") == "after\n"
    assert not (tmp_path / "delete.txt").exists()


def test_edit_file_preserves_unrelated_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"before old\r\nafter\r\n")
    registry = workspace_registry(tmp_path)

    result = approve(
        registry,
        call(
            "edit_file",
            {"path": "windows.txt", "old_text": "old", "new_text": "new"},
        ),
    )

    assert content(result)["ok"] is True
    assert target.read_bytes() == b"before new\r\nafter\r\n"


def test_patch_commit_rolls_back_even_when_temp_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    registry = workspace_registry(tmp_path)
    tools = registry._tools[0].tool  # type: ignore[attr-defined]
    patch_text = """*** Begin Patch
*** Update File: first.txt
@@
-one
+ONE
*** Update File: second.txt
@@
-two
+TWO
*** End Patch
"""
    plan = tools._plan_apply_patch(call("apply_patch", {"patch": patch_text}))  # type: ignore[attr-defined]
    real_rename = patching._rename_names
    real_unlink = patching._unlink_name
    calls = 0

    def failing_rename(parent: Any, source: str, destination: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected failure")
        real_rename(parent, source, destination)

    def failing_unlink(parent: Any, name: str) -> None:
        if ".tmp-" in name:
            raise ValueError("injected cleanup failure")
        real_unlink(parent, name)

    monkeypatch.setattr(patching, "_rename_names", failing_rename)
    monkeypatch.setattr(patching, "_unlink_name", failing_unlink)
    with pytest.raises(ValueError, match="failed to commit"):
        patching.commit_patch_changes(plan.changes, tools.evaluator)  # type: ignore[attr-defined]

    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptors are required")
def test_patch_commit_rolls_back_if_parent_is_swapped_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    parent = workspace / "nested"
    moved_parent = workspace / "moved"
    outside = tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()
    registry = workspace_registry(workspace)
    tools = registry._tools[0].tool  # type: ignore[attr-defined]
    plan = tools._plan_write_file(  # type: ignore[attr-defined]
        call("write_file", {"path": "nested/escape.txt", "content": "blocked"})
    )
    real_verify = patching._verify_unchanged

    def verify_then_swap(changes: Any, evaluator: Any, parents: Any) -> None:
        real_verify(changes, evaluator, parents)
        parent.rename(moved_parent)
        parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(patching, "_verify_unchanged", verify_then_swap)

    with pytest.raises(ValueError, match="parent directory .* changed"):
        patching.commit_patch_changes(plan.changes, tools.evaluator)  # type: ignore[attr-defined]

    assert not (outside / "escape.txt").exists()
    assert not (moved_parent / "escape.txt").exists()


def test_invalid_patch_grammar_is_rejected() -> None:
    with pytest.raises(ValueError, match="must start"):
        parse_patch("not a patch")
    with pytest.raises(ValueError, match="does not support move"):
        parse_patch("*** Begin Patch\n*** Move to: x\n*** End Patch")


def test_shell_timeout_and_cooperative_cancellation(tmp_path: Path) -> None:
    registry = ToolRegistry.built_in(
        tmp_path, PermissionProfile.for_mode(PermissionMode.DANGER_FULL_ACCESS)
    )

    started = time.monotonic()
    timed_out = content(
        run(registry.execute(call("shell_command", {"command": "sleep 2", "timeout_secs": 1})))
    )
    assert timed_out["data"]["timed_out"] is True
    assert time.monotonic() - started < 1.8

    async def cancel_command() -> ToolExecution:
        cancellation = CancellationToken()
        execution = asyncio.create_task(
            registry.execute(
                call("shell_command", {"command": "sleep 5", "timeout_secs": 5}),
                context=ToolExecutionContext(cancellation),
            )
        )
        await asyncio.sleep(0.05)
        cancellation.cancel()
        return await asyncio.wait_for(execution, 1)

    cancelled = content(run(cancel_command()))
    assert cancelled["error"] == "tool execution cancelled"

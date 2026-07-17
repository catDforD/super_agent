from __future__ import annotations

from pathlib import Path

import pytest

from morrow.protocol import PermissionMode, PermissionProfile, ShellPolicy
from morrow.sandbox import PermissionDecisionKind, PermissionEvaluator


def test_workspace_profiles_restrict_paths_and_prompt_for_shell(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    profile = PermissionProfile.for_mode(PermissionMode.WORKSPACE_WRITE)
    evaluator = PermissionEvaluator(tmp_path, profile)
    profile.mode = PermissionMode.DANGER_FULL_ACCESS

    with pytest.raises(ValueError, match="outside the workspace root"):
        evaluator.resolve_existing_path(str(outside))

    decision = evaluator.shell_command_decision("call_1", "pytest", 30)
    assert decision.kind is PermissionDecisionKind.PROMPT
    assert decision.request is not None
    assert decision.request.id == "approval-call_1"
    assert decision.request.action.cwd == tmp_path.resolve()


def test_write_resolution_rejects_read_only_and_symlink_escape(tmp_path: Path) -> None:
    read_only = PermissionEvaluator(tmp_path, PermissionProfile.for_mode(PermissionMode.READ_ONLY))
    with pytest.raises(ValueError, match="file writes are denied"):
        read_only.resolve_write_path("new.txt")

    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    writable = PermissionEvaluator(
        tmp_path, PermissionProfile.for_mode(PermissionMode.WORKSPACE_WRITE)
    )
    with pytest.raises(ValueError, match="outside the workspace root"):
        writable.resolve_write_path("escape/new.txt")


def test_danger_profile_allows_external_paths_and_shell_override(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-danger.txt"
    outside.write_text("ok", encoding="utf-8")
    evaluator = PermissionEvaluator(
        tmp_path,
        PermissionProfile(
            mode=PermissionMode.DANGER_FULL_ACCESS,
            shell=ShellPolicy.DENY,
        ),
    )

    assert evaluator.resolve_existing_path(str(outside)) == outside.resolve()
    assert evaluator.shell_command_decision("call", "pwd", 5).kind is PermissionDecisionKind.DENY

"""Workspace path and side-effect permission evaluation.

The evaluator deliberately resolves paths before tools inspect or mutate them.  Non-dangerous
profiles therefore cannot escape the workspace through absolute paths, ``..`` components, or
symbolic links.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from morrow.protocol import (
    ApprovalRequest,
    FileChangeSummary,
    PermissionMode,
    PermissionProfile,
    ShellPolicy,
)


class PermissionEvaluatorError(ValueError):
    """Raised when a permission evaluator cannot be constructed."""


class PermissionDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    kind: PermissionDecisionKind
    reason: str | None = None
    request: ApprovalRequest | None = None

    @classmethod
    def allow(cls) -> PermissionDecision:
        return cls(PermissionDecisionKind.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> PermissionDecision:
        return cls(PermissionDecisionKind.DENY, reason=reason)

    @classmethod
    def prompt(cls, request: ApprovalRequest) -> PermissionDecision:
        return cls(PermissionDecisionKind.PROMPT, request=request)


class PermissionEvaluator:
    """Resolve paths and decide whether shell/file side effects may proceed."""

    def __init__(self, root: str | Path, profile: PermissionProfile) -> None:
        candidate = Path(root).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise PermissionEvaluatorError(
                f"failed to canonicalize workspace root {candidate}: {exc}"
            ) from exc
        if not resolved.is_dir():
            raise PermissionEvaluatorError(f"workspace root {resolved} is not a directory")
        self._root = resolved
        self._profile = profile.model_copy(deep=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def profile(self) -> PermissionProfile:
        return self._profile.model_copy(deep=True)

    def allows_paths_outside_workspace(self) -> bool:
        return self._profile.mode == PermissionMode.DANGER_FULL_ACCESS

    def resolve_existing_path(self, input_path: str) -> Path:
        value = input_path.strip()
        if not value:
            raise ValueError("path must not be empty")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"failed to resolve path {value!r}: {exc}") from exc
        self._require_allowed(resolved, value)
        return resolved

    def resolve_write_path(self, input_path: str) -> Path:
        value = input_path.strip()
        if not value:
            raise ValueError("path must not be empty")
        if self._profile.mode == PermissionMode.READ_ONLY:
            raise ValueError(
                "file writes are denied by the active "
                f"{_enum_value(self._profile.mode)} permission profile"
            )

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        if candidate.name in {"", ".", ".."}:
            raise ValueError(f"path {value!r} must name a file")
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"failed to resolve parent directory for path {value!r}: {exc}"
            ) from exc
        if not parent.is_dir():
            raise ValueError(f"parent path {self.display_path(parent)} is not a directory")

        tentative = parent / candidate.name
        try:
            resolved = tentative.resolve(strict=True)
        except FileNotFoundError:
            resolved = tentative
        except OSError as exc:
            raise ValueError(f"failed to resolve path {value!r}: {exc}") from exc
        self._require_allowed(resolved, value)
        return resolved

    def validate_commit_path(self, path: Path, *, must_exist: bool) -> None:
        """Revalidate containment immediately before a staged mutation is committed."""

        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"failed to resolve parent directory for path {self.display_path(path)!r}: {exc}"
            ) from exc
        if parent != path.parent:
            raise ValueError(f"path {self.display_path(path)!r} changed since it was planned")
        candidate = parent / path.name
        if must_exist:
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    f"failed to resolve path {self.display_path(path)!r}: {exc}"
                ) from exc
            if candidate != path:
                raise ValueError(f"path {self.display_path(path)!r} changed since it was planned")
        self._require_allowed(candidate, self.display_path(path))

    def shell_command_decision(
        self, tool_call_id: str, command: str, timeout_secs: int
    ) -> PermissionDecision:
        if self._profile.shell == ShellPolicy.ALLOW:
            return PermissionDecision.allow()
        if self._profile.shell == ShellPolicy.DENY:
            return PermissionDecision.deny(
                "shell commands are denied by the active "
                f"{_enum_value(self._profile.mode)} permission profile"
            )
        return PermissionDecision.prompt(
            ApprovalRequest.shell_command(
                approval_id_for_tool_call(tool_call_id),
                command,
                self._root,
                timeout_secs,
                "shell command requires approval",
            )
        )

    def file_changes_decision(
        self,
        tool_call_id: str,
        files: list[FileChangeSummary],
        diff: str,
    ) -> PermissionDecision:
        if self._profile.mode == PermissionMode.DANGER_FULL_ACCESS:
            return PermissionDecision.allow()
        if self._profile.mode == PermissionMode.READ_ONLY:
            return PermissionDecision.deny(
                "file writes are denied by the active "
                f"{_enum_value(self._profile.mode)} permission profile"
            )
        return PermissionDecision.prompt(
            ApprovalRequest.file_changes(
                approval_id_for_tool_call(tool_call_id),
                files,
                diff,
                "file changes require approval",
            )
        )

    def display_path(self, path: str | Path) -> str:
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self._root)
        except ValueError:
            return str(candidate)
        return "." if str(relative) == "." else str(relative)

    def _require_allowed(self, path: Path, original: str) -> None:
        if self.allows_paths_outside_workspace():
            return
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"path {original!r} is outside the workspace root") from exc


def approval_id_for_tool_call(tool_call_id: str) -> str:
    return f"approval-{tool_call_id}"


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


__all__ = [
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionEvaluator",
    "PermissionEvaluatorError",
    "approval_id_for_tool_call",
]

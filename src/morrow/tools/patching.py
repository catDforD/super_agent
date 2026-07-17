"""Patch parsing and transactional file-change primitives used by built-in tools."""

from __future__ import annotations

import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from morrow.protocol import FileChangeOperation, FileChangeSummary
from morrow.sandbox import PermissionEvaluator

MAX_FILE_DIFF_LINES = 240
MAX_FILE_DIFF_BYTES = 20_000


class PatchOperationKind(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class PatchHunk:
    old_text: str
    new_text: str


@dataclass(frozen=True, slots=True)
class ParsedPatchOperation:
    kind: PatchOperationKind
    path: str
    content: str | None = None
    hunks: tuple[PatchHunk, ...] = ()


@dataclass(slots=True)
class StagedPatchChange:
    path: Path
    kind: PatchOperationKind
    content: str | None
    mode: int | None
    summary: FileChangeSummary
    before: str | None
    after: str | None
    temp_path: Path | None = field(default=None, repr=False)
    parent_identity: tuple[int, int] = field(init=False, repr=False)
    target_identity: tuple[int, int] | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        parent = os.stat(self.path.parent, follow_symlinks=False)
        self.parent_identity = (parent.st_dev, parent.st_ino)
        try:
            target = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            self.target_identity = None
        else:
            self.target_identity = (target.st_dev, target.st_ino)


@dataclass(slots=True)
class _AppliedChange:
    path: Path
    kind: PatchOperationKind
    backup_path: Path | None = None


@dataclass(slots=True)
class _SecureParent:
    path: Path
    identity: tuple[int, int]
    fd: int | None

    @classmethod
    def open(cls, path: Path, identity: tuple[int, int]) -> _SecureParent:
        if sys.platform == "win32" or os.name != "posix":
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode):
                raise ValueError(f"parent directory {path} changed since approval request")
            return cls(path=path, identity=identity, fd=None)
        required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        if any(not hasattr(os, name) for name in required):
            raise ValueError(
                "secure atomic file changes are unavailable on this platform; refusing write"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity or not stat.S_ISDIR(opened.st_mode):
            os.close(fd)
            raise ValueError(f"parent directory {path} changed since approval request")
        return cls(path=path, identity=identity, fd=fd)

    def ensure_anchored(self) -> None:
        try:
            resolved = self.path.resolve(strict=True)
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(
                f"parent directory {self.path} changed since approval request: {exc}"
            ) from exc
        if resolved != self.path:
            raise ValueError(f"parent directory {self.path} changed since approval request")
        if (current.st_dev, current.st_ino) != self.identity or not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"parent directory {self.path} changed since approval request")

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)


def parse_patch(patch: str) -> list[ParsedPatchOperation]:
    normalized = patch.replace("\r\n", "\n")
    lines = normalized.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1] != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")
    if len(lines) <= 2:
        raise ValueError("patch must contain at least one operation")

    end = len(lines) - 1
    index = 1
    operations: list[ParsedPatchOperation] = []
    while index < end:
        line = lines[index]
        if line.startswith("*** Move to:"):
            raise ValueError("apply_patch does not support move operations")
        if line.startswith("*** Add File: "):
            path = _parse_patch_path(line.removeprefix("*** Add File: "))
            index += 1
            content: list[str] = []
            while index < end and not _is_patch_directive(lines[index]):
                current = lines[index]
                if not current.startswith("+"):
                    raise ValueError(f"invalid add file line for {path}; expected + prefix")
                content.append(current[1:])
                index += 1
            if not content:
                raise ValueError(f"add file {path} must contain at least one line")
            operations.append(
                ParsedPatchOperation(
                    PatchOperationKind.ADD, path, content="\n".join(content) + "\n"
                )
            )
            continue

        if line.startswith("*** Update File: "):
            path = _parse_patch_path(line.removeprefix("*** Update File: "))
            index += 1
            hunks: list[PatchHunk] = []
            while index < end and not _is_patch_directive(lines[index]):
                if not lines[index].startswith("@@"):
                    raise ValueError(f"expected @@ hunk header for update file {path}")
                index += 1
                old_lines: list[str] = []
                new_lines: list[str] = []
                line_count = 0
                while (
                    index < end
                    and not lines[index].startswith("@@")
                    and not _is_patch_directive(lines[index])
                ):
                    current = lines[index]
                    if not current:
                        raise ValueError(f"invalid empty hunk line for update file {path}")
                    prefix, payload = current[0], current[1:]
                    if prefix == " ":
                        old_lines.append(payload)
                        new_lines.append(payload)
                    elif prefix == "-":
                        old_lines.append(payload)
                    elif prefix == "+":
                        new_lines.append(payload)
                    else:
                        raise ValueError(
                            f"invalid hunk line prefix {prefix!r} for update file {path}"
                        )
                    line_count += 1
                    index += 1
                if line_count == 0:
                    raise ValueError(f"empty hunk for update file {path}")
                old_text = "\n".join(old_lines) + ("\n" if old_lines else "")
                new_text = "\n".join(new_lines) + ("\n" if new_lines else "")
                if not old_text:
                    raise ValueError(
                        f"hunk for update file {path} must include context or removed lines"
                    )
                if old_text == new_text:
                    raise ValueError(f"hunk for update file {path} has no changes")
                hunks.append(PatchHunk(old_text, new_text))
            if not hunks:
                raise ValueError(f"update file {path} must contain at least one hunk")
            operations.append(
                ParsedPatchOperation(PatchOperationKind.UPDATE, path, hunks=tuple(hunks))
            )
            continue

        if line.startswith("*** Delete File: "):
            path = _parse_patch_path(line.removeprefix("*** Delete File: "))
            operations.append(ParsedPatchOperation(PatchOperationKind.DELETE, path))
            index += 1
            continue

        if line.startswith("*** "):
            raise ValueError(f"unknown patch operation {line!r}")
        raise ValueError(f"expected patch operation, found {line!r}")
    return operations


def plan_patch_changes(
    operations: list[ParsedPatchOperation], evaluator: PermissionEvaluator
) -> list[StagedPatchChange]:
    paths: set[Path] = set()
    changes: list[StagedPatchChange] = []
    for operation in operations:
        path = evaluator.resolve_write_path(operation.path)
        if path in paths:
            raise ValueError(f"patch modifies {evaluator.display_path(path)} more than once")
        paths.add(path)
        display = evaluator.display_path(path)

        if operation.kind is PatchOperationKind.ADD:
            if path.exists() or path.is_symlink():
                raise ValueError(f"{display} already exists; add file cannot overwrite it")
            changes.append(
                StagedPatchChange(
                    path=path,
                    kind=operation.kind,
                    content=operation.content,
                    mode=None,
                    summary=FileChangeSummary(
                        path=display,
                        operation=FileChangeOperation.ADD,
                        replacements=0,
                        created=True,
                        overwritten=False,
                        deleted=False,
                    ),
                    before=None,
                    after=operation.content,
                )
            )
            continue

        try:
            stat = path.stat()
        except OSError as exc:
            raise ValueError(f"failed to inspect {display}: {exc}") from exc
        if not path.is_file():
            raise ValueError(f"{display} is not a file")
        try:
            original = _read_utf8(path)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"failed to read {display} as UTF-8 text: {exc}") from exc

        if operation.kind is PatchOperationKind.UPDATE:
            updated = original
            replacements = 0
            for hunk in operation.hunks:
                matches = updated.count(hunk.old_text)
                if matches != 1:
                    raise ValueError(
                        f"patch hunk for {display} must match exactly once; found {matches}"
                    )
                updated = updated.replace(hunk.old_text, hunk.new_text, 1)
                replacements += 1
            if updated == original:
                raise ValueError(f"patch update for {display} did not change file content")
            changes.append(
                StagedPatchChange(
                    path=path,
                    kind=operation.kind,
                    content=updated,
                    mode=stat.st_mode,
                    summary=FileChangeSummary(
                        path=display,
                        operation=FileChangeOperation.UPDATE,
                        replacements=replacements,
                        created=False,
                        overwritten=True,
                        deleted=False,
                    ),
                    before=original,
                    after=updated,
                )
            )
        else:
            changes.append(
                StagedPatchChange(
                    path=path,
                    kind=operation.kind,
                    content=None,
                    mode=None,
                    summary=FileChangeSummary(
                        path=display,
                        operation=FileChangeOperation.DELETE,
                        replacements=0,
                        created=False,
                        overwritten=False,
                        deleted=True,
                    ),
                    before=original,
                    after=None,
                )
            )
    return changes


def render_file_diff(changes: list[StagedPatchChange], evaluator: PermissionEvaluator) -> str:
    output: list[str] = []
    byte_count = 0
    truncated = False

    def push(line: str) -> None:
        nonlocal byte_count, truncated
        if truncated:
            return
        encoded = (line + "\n").encode("utf-8")
        if len(output) >= MAX_FILE_DIFF_LINES or byte_count + len(encoded) > MAX_FILE_DIFF_BYTES:
            truncated = True
            return
        output.append(line)
        byte_count += len(encoded)

    for change in changes:
        path = evaluator.display_path(change.path)
        push(f"--- {'/dev/null' if change.kind is PatchOperationKind.ADD else path}")
        push(f"+++ {'/dev/null' if change.kind is PatchOperationKind.DELETE else path}")
        push("@@")
        if change.before is not None:
            for line in change.before.splitlines():
                push(f"-{line}")
        if change.after is not None:
            for line in change.after.splitlines():
                push(f"+{line}")
        push("")
    if truncated:
        output.append("... diff truncated ...")
    return "\n".join(output) + ("\n" if output else "")


def _open_secure_parents(
    changes: list[StagedPatchChange],
) -> dict[Path, _SecureParent]:
    identities: dict[Path, tuple[int, int]] = {}
    for change in changes:
        previous = identities.setdefault(change.path.parent, change.parent_identity)
        if previous != change.parent_identity:
            raise ValueError(
                f"parent directory {change.path.parent} changed while planning file changes"
            )
    opened: dict[Path, _SecureParent] = {}
    try:
        for path, identity in identities.items():
            opened[path] = _SecureParent.open(path, identity)
    except Exception:
        for parent in opened.values():
            with suppress(OSError):
                parent.close()
        raise
    return opened


def commit_patch_changes(changes: list[StagedPatchChange], evaluator: PermissionEvaluator) -> None:
    """Install all changes or restore every original on failure."""

    parents = _open_secure_parents(changes)
    try:
        _verify_unchanged(changes, evaluator, parents)
        try:
            for change in changes:
                if change.content is None:
                    continue
                change.temp_path = _write_temp(change, parents[change.path.parent])
        except Exception:
            _cleanup_temps(changes, parents)
            raise

        applied: list[_AppliedChange] = []
        try:
            for change in changes:
                parent = parents[change.path.parent]
                if change.kind is PatchOperationKind.ADD:
                    assert change.temp_path is not None
                    _link_names(parent, change.temp_path.name, change.path.name)
                    applied.append(_AppliedChange(change.path, change.kind))
                    _unlink_name(parent, change.temp_path.name)
                    change.temp_path = None
                    continue

                backup = _reserve_path(change.path, "bak", parent)
                _rename_names(parent, change.path.name, backup.name)
                applied.append(_AppliedChange(change.path, change.kind, backup))
                if change.kind is PatchOperationKind.UPDATE:
                    assert change.temp_path is not None
                    _rename_names(parent, change.temp_path.name, change.path.name)
                    change.temp_path = None
            for parent in parents.values():
                parent.ensure_anchored()
        except Exception as exc:
            _cleanup_temps(changes, parents)
            rollback_errors = _rollback(applied, evaluator, parents)
            message = _commit_error(exc, changes, evaluator)
            if rollback_errors:
                message += "; rollback errors: " + "; ".join(rollback_errors)
            raise ValueError(message) from exc
        for installed in applied:
            if installed.backup_path is not None:
                # The transaction is already committed. A stale hidden backup is preferable to
                # rolling back successfully installed user changes.
                with suppress(Exception):
                    parent = parents[installed.path.parent]
                    _unlink_name(parent, installed.backup_path.name)
    finally:
        for parent in parents.values():
            with suppress(OSError):
                parent.close()


def file_change_summary_json(summary: FileChangeSummary) -> dict[str, object]:
    operation = getattr(summary.operation, "value", summary.operation)
    return {
        "path": summary.path,
        "operation": str(operation),
        "replacements": summary.replacements,
        "created": summary.created,
        "overwritten": summary.overwritten,
        "deleted": summary.deleted,
    }


def _verify_unchanged(
    changes: list[StagedPatchChange],
    evaluator: PermissionEvaluator,
    parents: dict[Path, _SecureParent],
) -> None:
    for change in changes:
        display = evaluator.display_path(change.path)
        must_exist = change.kind is not PatchOperationKind.ADD
        evaluator.validate_commit_path(change.path, must_exist=must_exist)
        parent = parents[change.path.parent]
        parent.ensure_anchored()
        if change.kind is PatchOperationKind.ADD:
            try:
                _stat_name(parent, change.path.name)
            except FileNotFoundError:
                pass
            else:
                raise ValueError(f"{display} changed since approval request")
            continue
        try:
            descriptor = _open_name(
                parent,
                change.path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise ValueError(f"failed to verify {display} before commit: {exc}") from exc
        try:
            current_stat = os.fstat(descriptor)
            identity = (current_stat.st_dev, current_stat.st_ino)
            if identity != change.target_identity or not stat.S_ISREG(current_stat.st_mode):
                raise ValueError(f"{display} changed since approval request")
            current = _read_utf8_descriptor(descriptor)
        finally:
            os.close(descriptor)
        if current != change.before:
            raise ValueError(f"{display} changed since approval request")


def _write_temp(change: StagedPatchChange, parent: _SecureParent) -> Path:
    name, descriptor = _create_unique_file(parent, change.path.name, "tmp")
    temp = parent.path / name
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(change.content or "")
            if change.mode is not None:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), change.mode)
                else:  # pragma: no cover - Windows
                    os.chmod(temp, change.mode)
        return temp
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(Exception):
            _unlink_name(parent, name)
        raise


def _read_utf8(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _read_utf8_descriptor(descriptor: int) -> str:
    with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _create_unique_file(parent: _SecureParent, target_name: str, label: str) -> tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(128):
        name = f".{target_name}.{label}-{os.getpid()}-{secrets.token_hex(8)}"
        try:
            descriptor = _open_name(parent, name, flags, 0o600)
        except FileExistsError:
            continue
        return name, descriptor
    raise FileExistsError(f"failed to reserve a unique {label} file for {target_name}")


def _stat_name(parent: _SecureParent, name: str) -> os.stat_result:
    if parent.fd is None:
        parent.ensure_anchored()
        return os.stat(parent.path / name, follow_symlinks=False)
    return os.stat(name, dir_fd=parent.fd, follow_symlinks=False)


def _open_name(parent: _SecureParent, name: str, flags: int, mode: int = 0o777) -> int:
    if parent.fd is None:
        parent.ensure_anchored()
        return os.open(parent.path / name, flags, mode)
    return os.open(name, flags, mode, dir_fd=parent.fd)


def _link_names(parent: _SecureParent, source: str, destination: str) -> None:
    if parent.fd is None:
        parent.ensure_anchored()
        if os.name == "nt":  # os.rename is atomic and no-clobber on Windows.
            os.rename(parent.path / source, parent.path / destination)
        else:  # pragma: no cover - non-POSIX, non-Windows compatibility path
            os.link(
                parent.path / source,
                parent.path / destination,
                follow_symlinks=False,
            )
    else:
        os.link(
            source,
            destination,
            src_dir_fd=parent.fd,
            dst_dir_fd=parent.fd,
            follow_symlinks=False,
        )


def _rename_names(parent: _SecureParent, source: str, destination: str) -> None:
    if parent.fd is None:
        parent.ensure_anchored()
        os.replace(parent.path / source, parent.path / destination)
    else:
        os.rename(source, destination, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)


def _unlink_name(parent: _SecureParent, name: str) -> None:
    with suppress(FileNotFoundError):
        if parent.fd is None:
            parent.ensure_anchored()
            (parent.path / name).unlink()
        else:
            os.unlink(name, dir_fd=parent.fd)


def _reserve_path(path: Path, label: str, parent: _SecureParent) -> Path:
    name, descriptor = _create_unique_file(parent, path.name, label)
    os.close(descriptor)
    return parent.path / name


def _cleanup_temps(changes: list[StagedPatchChange], parents: dict[Path, _SecureParent]) -> None:
    for change in changes:
        if change.temp_path is not None:
            # Cleanup is best effort and must never prevent the transaction rollback below.
            with suppress(Exception):
                _unlink_name(parents[change.path.parent], change.temp_path.name)


def _rollback(
    applied: list[_AppliedChange],
    evaluator: PermissionEvaluator,
    parents: dict[Path, _SecureParent],
) -> list[str]:
    errors: list[str] = []
    for change in reversed(applied):
        display = evaluator.display_path(change.path)
        parent = parents[change.path.parent]
        try:
            if change.kind is PatchOperationKind.ADD:
                _unlink_name(parent, change.path.name)
            elif change.kind is PatchOperationKind.UPDATE:
                _unlink_name(parent, change.path.name)
                assert change.backup_path is not None
                _rename_names(parent, change.backup_path.name, change.path.name)
            else:
                assert change.backup_path is not None
                _rename_names(parent, change.backup_path.name, change.path.name)
        except Exception as exc:  # pragma: no cover - exercised via fault injection
            errors.append(f"failed to restore {display}: {exc}")
    return errors


def _commit_error(
    exc: Exception,
    changes: list[StagedPatchChange],
    evaluator: PermissionEvaluator,
) -> str:
    if isinstance(exc, FileExistsError):
        for change in changes:
            if change.kind is PatchOperationKind.ADD and change.path.exists():
                return (
                    f"{evaluator.display_path(change.path)} already exists; "
                    "add file cannot overwrite it"
                )
    return f"failed to commit file changes: {exc}"


def _parse_patch_path(path: str) -> str:
    value = path.strip()
    if not value:
        raise ValueError("patch operation path must not be empty")
    return value


def _is_patch_directive(line: str) -> bool:
    return line.startswith("*** ")


__all__ = [
    "ParsedPatchOperation",
    "PatchHunk",
    "PatchOperationKind",
    "StagedPatchChange",
    "commit_patch_changes",
    "file_change_summary_json",
    "parse_patch",
    "plan_patch_changes",
    "render_file_diff",
]

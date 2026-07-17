"""The seven built-in Morrow tools."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from morrow.core import (
    ToolApproval,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
    ToolResult,
)
from morrow.protocol import (
    ApprovalDecision,
    ApprovalRequest,
    FileChangeOperation,
    FileChangeSummary,
    ShellCommandSummary,
    ToolCall,
    ToolDefinition,
    ToolExecutionSummary,
)
from morrow.sandbox import (
    PermissionDecisionKind,
    PermissionEvaluator,
)

from .patching import (
    PatchOperationKind,
    StagedPatchChange,
    commit_patch_changes,
    file_change_summary_json,
    parse_patch,
    plan_patch_changes,
    render_file_diff,
)

DEFAULT_READ_LINES = 200
MAX_READ_LINES = 1000
DEFAULT_LIST_ENTRIES = 100
MAX_LIST_ENTRIES = 500
DEFAULT_SEARCH_RESULTS = 100
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_LINE_CHARS = 500
MAX_SEARCH_TOTAL_BYTES = 20_000
DEFAULT_SHELL_TIMEOUT_SECS = 30
MAX_SHELL_TIMEOUT_SECS = 120
MAX_SHELL_OUTPUT_BYTES = 20_000
OUTPUT_DRAIN_TIMEOUT_SECS = 0.25
TOOL_CANCELLED_ERROR = "tool execution cancelled"
SEARCH_SKIP_NAMES = frozenset({".git", "node_modules", "dist", "build", "target"})


@dataclass(slots=True)
class FileChangePlan:
    changes: list[StagedPatchChange]
    data: dict[str, Any]
    files: list[FileChangeSummary]
    diff: str
    summary: ToolExecutionSummary


@dataclass(slots=True)
class _SearchOutput:
    query: str
    path: str
    case_sensitive: bool
    max_results: int
    total_result_bytes: int = 0
    result_truncated: bool = False
    results: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.results = []

    def push_match(self, path: str, line: int, text: str) -> bool:
        assert self.results is not None
        if len(self.results) >= self.max_results:
            self.result_truncated = True
            return False
        text = text.rstrip("\r\n")
        text_truncated = len(text) > MAX_SEARCH_LINE_CHARS
        if text_truncated:
            text = text[:MAX_SEARCH_LINE_CHARS]
        item = {
            "path": path,
            "line": line,
            "text": text,
            "text_truncated": text_truncated,
        }
        item_bytes = len(_json_dumps(item).encode("utf-8"))
        if self.total_result_bytes + item_bytes > MAX_SEARCH_TOTAL_BYTES:
            self.result_truncated = True
            return False
        self.total_result_bytes += item_bytes
        self.results.append(item)
        return True

    def into_value(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "path": self.path,
            "case_sensitive": self.case_sensitive,
            "truncated": self.result_truncated,
            "result_truncated": self.result_truncated,
            "results": self.results,
        }


class BuiltInTools:
    def __init__(self, evaluator: PermissionEvaluator) -> None:
        self.evaluator = evaluator

    def definitions(self) -> list[ToolDefinition]:
        return built_in_definitions()

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
        if call.function.name in {"edit_file", "write_file", "apply_patch", "shell_command"}:
            return ToolExecutionMode.SERIAL
        return ToolExecutionMode.CONCURRENT

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution:
        context = context or ToolExecutionContext()
        cancellation = context.cancellation
        if _is_cancelled(cancellation):
            return ToolExecution.error(TOOL_CANCELLED_ERROR)
        if call.function.name == "shell_command":
            return await self._shell_command(call, approval, cancellation)
        # Match the Rust spawn_blocking boundary: safe read tools can truly run concurrently and
        # large directory walks do not stall model streaming or WebSocket handling. File changes
        # remain serial at the core scheduler and re-check cancellation immediately before commit.
        return await asyncio.to_thread(self._execute_blocking, call, approval, cancellation)

    def _execute_blocking(
        self, call: ToolCall, approval: ToolApproval | None, cancellation: Any
    ) -> ToolExecution:
        if _is_cancelled(cancellation):
            return ToolExecution.error(TOOL_CANCELLED_ERROR)
        try:
            if call.function.name == "read_file":
                return ToolExecution.completed(_tool_ok(self._read_file(call)))
            if call.function.name == "list_files":
                return ToolExecution.completed(_tool_ok(self._list_files(call)))
            if call.function.name == "search_text":
                return ToolExecution.completed(_tool_ok(self._search_text(call)))
            if call.function.name == "edit_file":
                return self._execute_file_change_plan(
                    call, self._plan_edit_file(call), approval, cancellation
                )
            if call.function.name == "write_file":
                return self._execute_file_change_plan(
                    call, self._plan_write_file(call), approval, cancellation
                )
            if call.function.name == "apply_patch":
                return self._execute_file_change_plan(
                    call, self._plan_apply_patch(call), approval, cancellation
                )
            return ToolExecution.error(f"unknown tool {call.function.name!r}")
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            return ToolExecution.error(str(exc))

    def _read_file(self, call: ToolCall) -> dict[str, Any]:
        args = _parse_args(call, {"path"}, {"start_line", "max_lines"})
        path_arg = _require_str(args, "path")
        start_line = _optional_int(args, "start_line", 1)
        if start_line < 1:
            raise ValueError("start_line must be at least 1")
        max_lines = _clamp_limit(args.get("max_lines"), DEFAULT_READ_LINES, MAX_READ_LINES)
        path = self.evaluator.resolve_existing_path(path_arg)
        if not path.is_file():
            raise ValueError(f"{self.evaluator.display_path(path)} is not a file")
        try:
            content = _read_utf8(path)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"failed to read {self.evaluator.display_path(path)}: {exc}") from exc
        lines = _text_lines(content)
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        end_line = start_line + len(selected) - 1 if selected else None
        truncated = start_line - 1 + len(selected) < len(lines)
        return {
            "path": self.evaluator.display_path(path),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "truncated": truncated,
            "content": "\n".join(selected),
        }

    def _list_files(self, call: ToolCall) -> dict[str, Any]:
        args = _parse_args(call, set(), {"path", "recursive", "max_entries"})
        path_arg = _optional_str(args, "path", ".")
        recursive = _optional_bool(args, "recursive", False)
        max_entries = _clamp_limit(args.get("max_entries"), DEFAULT_LIST_ENTRIES, MAX_LIST_ENTRIES)
        path = self.evaluator.resolve_existing_path(path_arg)
        if not path.is_dir():
            raise ValueError(f"{self.evaluator.display_path(path)} is not a directory")
        entries: list[dict[str, str]] = []
        truncated = self._collect_entries(path, recursive, max_entries, entries)
        return {
            "path": self.evaluator.display_path(path),
            "recursive": recursive,
            "truncated": truncated,
            "entries": entries,
        }

    def _collect_entries(
        self,
        directory: Path,
        recursive: bool,
        max_entries: int,
        entries: list[dict[str, str]],
    ) -> bool:
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ValueError(
                f"failed to list {self.evaluator.display_path(directory)}: {exc}"
            ) from exc
        for child in children:
            if child.name in SEARCH_SKIP_NAMES:
                continue
            if len(entries) >= max_entries:
                return True
            try:
                is_link = child.is_symlink()
                resolved = child.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"failed to resolve listed path: {exc}") from exc
            if not self._path_allowed(resolved):
                continue
            if is_link:
                kind = "other"
                is_directory = False
            elif resolved.is_dir():
                kind = "directory"
                is_directory = True
            elif resolved.is_file():
                kind = "file"
                is_directory = False
            else:
                kind = "other"
                is_directory = False
            entries.append({"path": self.evaluator.display_path(resolved), "kind": kind})
            if (
                recursive
                and is_directory
                and self._collect_entries(resolved, True, max_entries, entries)
            ):
                return True
        return False

    def _search_text(self, call: ToolCall) -> dict[str, Any]:
        args = _parse_args(call, {"query"}, {"path", "case_sensitive", "max_results"})
        query = _require_str(args, "query")
        if not query:
            raise ValueError("query must not be empty")
        path_arg = _optional_str(args, "path", ".")
        case_sensitive = _optional_bool(args, "case_sensitive", False)
        max_results = _clamp_limit(
            args.get("max_results"), DEFAULT_SEARCH_RESULTS, MAX_SEARCH_RESULTS
        )
        path = self.evaluator.resolve_existing_path(path_arg)
        output = _SearchOutput(
            query,
            self.evaluator.display_path(path),
            case_sensitive,
            max_results,
        )
        ripgrep = shutil.which("rg")
        if ripgrep is not None:
            try:
                self._search_with_ripgrep(ripgrep, path, output)
                return output.into_value()
            except FileNotFoundError:
                pass
        self._search_fallback(path, output)
        return output.into_value()

    def _search_with_ripgrep(self, ripgrep: str, path: Path, output: _SearchOutput) -> None:
        search_path = self.evaluator.display_path(path)
        command = [
            ripgrep,
            "--json",
            "--fixed-strings",
            "--color",
            "never",
            "--no-messages",
        ]
        if not output.case_sensitive:
            command.append("--ignore-case")
        for skipped in sorted(SEARCH_SKIP_NAMES):
            command.extend(("--glob", f"!**/{skipped}/**", "--glob", f"!{skipped}/**"))
        command.extend(("--", output.query, search_path))
        try:
            process = subprocess.Popen(
                command,
                cwd=self.evaluator.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"failed to start ripgrep: {exc}") from exc
        assert process.stdout is not None
        stopped_early = False
        try:
            for frame in process.stdout:
                match = _parse_ripgrep_match(frame)
                if match is None:
                    continue
                raw_path, line_number, text = match
                match_path = Path(raw_path)
                if not match_path.is_absolute():
                    match_path = self.evaluator.root / match_path
                if not output.push_match(
                    self.evaluator.display_path(match_path), line_number, text
                ):
                    stopped_early = True
                    process.kill()
                    break
        finally:
            process.stdout.close()
        status = process.wait()
        if not stopped_early and status not in (0, 1):
            raise ValueError(f"ripgrep search failed with status {status}")

    def _search_fallback(self, path: Path, output: _SearchOutput) -> None:
        if path.is_file():
            self._search_file(path, output, fail_on_read_error=True)
            return
        if not path.is_dir():
            raise ValueError(f"{self.evaluator.display_path(path)} is not searchable")
        for file_path in self._collect_search_files(path):
            self._search_file(file_path, output, fail_on_read_error=False)
            if output.result_truncated:
                return

    def _collect_search_files(self, directory: Path) -> list[Path]:
        files: list[Path] = []
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ValueError(
                f"failed to list {self.evaluator.display_path(directory)}: {exc}"
            ) from exc
        for child in children:
            if child.name in SEARCH_SKIP_NAMES:
                continue
            try:
                is_link = child.is_symlink()
                resolved = child.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"failed to resolve search path: {exc}") from exc
            if not self._path_allowed(resolved):
                continue
            if not is_link and resolved.is_dir():
                files.extend(self._collect_search_files(resolved))
            elif resolved.is_file():
                files.append(resolved)
        return files

    def _search_file(self, path: Path, output: _SearchOutput, *, fail_on_read_error: bool) -> None:
        try:
            content = _read_utf8(path)
        except (OSError, UnicodeError) as exc:
            if fail_on_read_error:
                raise ValueError(
                    f"failed to read {self.evaluator.display_path(path)} as UTF-8 text: {exc}"
                ) from exc
            return
        needle = output.query if output.case_sensitive else output.query.lower()
        for index, line in enumerate(_text_lines(content), start=1):
            haystack = line if output.case_sensitive else line.lower()
            if needle in haystack and not output.push_match(
                self.evaluator.display_path(path), index, line
            ):
                return

    def _plan_edit_file(self, call: ToolCall) -> FileChangePlan:
        args = _parse_args(call, {"path", "old_text", "new_text"}, set())
        path_arg = _require_str(args, "path")
        old_text = _require_str(args, "old_text")
        new_text = _require_str(args, "new_text")
        if not old_text:
            raise ValueError("old_text must not be empty")
        path = self.evaluator.resolve_write_path(path_arg)
        display = self.evaluator.display_path(path)
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise ValueError(f"failed to inspect {display}: {exc}") from exc
        if not path.is_file():
            raise ValueError(f"{display} is not a file")
        try:
            content = _read_utf8(path)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"failed to read {display} as UTF-8 text: {exc}") from exc
        replacements = content.count(old_text)
        if replacements != 1:
            raise ValueError(f"old_text must match exactly once in {display}; found {replacements}")
        updated = content.replace(old_text, new_text, 1)
        summary = FileChangeSummary(
            path=display,
            operation=FileChangeOperation.UPDATE,
            replacements=1,
            created=False,
            overwritten=True,
            deleted=False,
        )
        change = StagedPatchChange(
            path,
            PatchOperationKind.UPDATE,
            updated,
            stat_result.st_mode,
            summary,
            content,
            updated,
        )
        return self._file_change_plan(
            [change],
            {
                "path": display,
                "replacements": 1,
                "created": False,
                "overwritten": True,
            },
        )

    def _plan_write_file(self, call: ToolCall) -> FileChangePlan:
        args = _parse_args(call, {"path", "content"}, {"overwrite"})
        path_arg = _require_str(args, "path")
        content = _require_str(args, "content")
        overwrite = _optional_bool(args, "overwrite", False)
        path = self.evaluator.resolve_write_path(path_arg)
        display = self.evaluator.display_path(path)
        exists = path.exists() or path.is_symlink()
        if exists and not path.is_file():
            raise ValueError(f"{display} is not a file")
        if exists and not overwrite:
            raise ValueError(f"{display} already exists; set overwrite to true to replace it")
        original: str | None = None
        mode: int | None = None
        if exists:
            try:
                mode = path.stat().st_mode
                original = _read_utf8(path)
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"failed to read {display} as UTF-8 text: {exc}") from exc
        created = not exists
        summary = FileChangeSummary(
            path=display,
            operation=(FileChangeOperation.ADD if created else FileChangeOperation.UPDATE),
            replacements=0,
            created=created,
            overwritten=exists,
            deleted=False,
        )
        change = StagedPatchChange(
            path,
            PatchOperationKind.ADD if created else PatchOperationKind.UPDATE,
            content,
            mode,
            summary,
            original,
            content,
        )
        return self._file_change_plan(
            [change],
            {
                "path": display,
                "replacements": 0,
                "created": created,
                "overwritten": exists,
            },
        )

    def _plan_apply_patch(self, call: ToolCall) -> FileChangePlan:
        args = _parse_args(call, {"patch"}, set())
        patch = _require_str(args, "patch")
        changes = plan_patch_changes(parse_patch(patch), self.evaluator)
        files = [file_change_summary_json(change.summary) for change in changes]
        return self._file_change_plan(changes, {"changed_files": len(files), "files": files})

    def _file_change_plan(
        self, changes: list[StagedPatchChange], data: dict[str, Any]
    ) -> FileChangePlan:
        files = [change.summary for change in changes]
        diff = render_file_diff(changes, self.evaluator)
        return FileChangePlan(
            changes,
            data,
            files,
            diff,
            ToolExecutionSummary.file_changes(files, diff),
        )

    def _execute_file_change_plan(
        self,
        call: ToolCall,
        plan: FileChangePlan,
        approval: ToolApproval | None,
        cancellation: Any,
    ) -> ToolExecution:
        if _is_cancelled(cancellation):
            return ToolExecution.error(TOOL_CANCELLED_ERROR)
        decision = self.evaluator.file_changes_decision(call.id, plan.files, plan.diff)
        if decision.kind is PermissionDecisionKind.DENY:
            return ToolExecution.error(decision.reason or "file changes denied")
        if decision.kind is PermissionDecisionKind.PROMPT:
            assert decision.request is not None
            if approval is None:
                return ToolExecution.approval_required(decision.request)
            mismatch = _validate_approval(
                approval,
                decision.request,
                stale_error=(
                    "file changes changed since approval request; approval no longer matches "
                    "planned changes"
                ),
            )
            if mismatch is not None:
                return ToolExecution.error(mismatch)
            if not approval.decision.approved:
                return ToolExecution.error("file changes approval denied")
        if _is_cancelled(cancellation):
            return ToolExecution.error(TOOL_CANCELLED_ERROR)
        try:
            commit_patch_changes(plan.changes, self.evaluator)
        except (OSError, ValueError) as exc:
            return ToolExecution.error(str(exc))
        return ToolExecution.completed(_tool_ok(plan.data, plan.summary))

    async def _shell_command(
        self, call: ToolCall, approval: ToolApproval | None, cancellation: Any
    ) -> ToolExecution:
        try:
            args = _parse_args(call, {"command"}, {"timeout_secs"})
            command = _require_str(args, "command")
            if not command.strip():
                raise ValueError("command must not be empty")
            timeout_secs = _optional_int(args, "timeout_secs", DEFAULT_SHELL_TIMEOUT_SECS)
            timeout_secs = min(timeout_secs, MAX_SHELL_TIMEOUT_SECS)
            if timeout_secs < 1:
                raise ValueError("timeout_secs must be at least 1")
        except (TypeError, ValueError) as exc:
            return ToolExecution.error(str(exc))

        decision = self.evaluator.shell_command_decision(call.id, command, timeout_secs)
        if decision.kind is PermissionDecisionKind.DENY:
            return ToolExecution.error(decision.reason or "shell command denied")
        if decision.kind is PermissionDecisionKind.PROMPT:
            assert decision.request is not None
            if approval is None:
                return ToolExecution.approval_required(decision.request)
            mismatch = _validate_approval(
                approval,
                decision.request,
                stale_error=(
                    "shell command changed since approval request; approval no longer matches "
                    "planned command"
                ),
            )
            if mismatch is not None:
                return ToolExecution.error(mismatch)
            if not approval.decision.approved:
                return ToolExecution.error("shell command approval denied")
        try:
            data, shell_summary = await run_shell_command(
                self.evaluator.root, command, timeout_secs, cancellation
            )
        except (OSError, ValueError) as exc:
            return ToolExecution.error(str(exc))
        return ToolExecution.completed(_tool_ok(data, ToolExecutionSummary.shell(shell_summary)))

    def _path_allowed(self, path: Path) -> bool:
        if self.evaluator.allows_paths_outside_workspace():
            return True
        try:
            path.relative_to(self.evaluator.root)
            return True
        except ValueError:
            return False


def built_in_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition.function(
            "read_file",
            "Read a UTF-8 text file from the workspace.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_READ_LINES,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "list_files",
            "List files and directories under the workspace.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIST_ENTRIES,
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "search_text",
            "Search workspace text files for a literal string.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_RESULTS,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "edit_file",
            "Edit a UTF-8 text file by replacing text that matches exactly once.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "write_file",
            "Create or overwrite a UTF-8 text file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "apply_patch",
            "Apply a patch to add, update, or delete files.",
            {
                "type": "object",
                "properties": {"patch": {"type": "string", "description": "Patch text to apply."}},
                "required": ["patch"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "shell_command",
            "Run a shell command in the workspace root with a timeout.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_secs": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SHELL_TIMEOUT_SECS,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
    ]


async def run_shell_command(
    root: Path, command: str, timeout_secs: int, cancellation: Any
) -> tuple[dict[str, Any], ShellCommandSummary]:
    if _is_cancelled(cancellation):
        raise ValueError(TOOL_CANCELLED_ERROR)
    kwargs: dict[str, Any] = {
        "cwd": root,
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    process = await asyncio.create_subprocess_shell(command, **kwargs)
    stdout_task = asyncio.create_task(_capture_output(process.stdout))
    stderr_task = asyncio.create_task(_capture_output(process.stderr))
    completion = asyncio.ensure_future(asyncio.gather(process.wait(), stdout_task, stderr_task))
    cancellation_task = asyncio.create_task(_wait_cancelled(cancellation))
    timed_out = False
    cancelled = False
    try:
        waiters: set[asyncio.Future[Any]] = {completion, cancellation_task}
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout_secs,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if completion in done:
            cancellation_task.cancel()
        elif cancellation_task in done:
            cancelled = True
            _kill_process(process)
        else:
            timed_out = True
            _kill_process(process)
        if not completion.done():
            try:
                await asyncio.wait_for(asyncio.shield(completion), OUTPUT_DRAIN_TIMEOUT_SECS)
            except TimeoutError:
                completion.cancel()
        stdout, stdout_truncated = _task_output(stdout_task)
        stderr, stderr_truncated = _task_output(stderr_task)
        if cancelled:
            raise ValueError(TOOL_CANCELLED_ERROR)
        exit_code = (
            process.returncode
            if process.returncode is not None and process.returncode >= 0
            else None
        )
        data = {
            "command": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        summary = ShellCommandSummary(
            command=command,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
        return data, summary
    except asyncio.CancelledError:
        _kill_process(process)
        if not completion.done():
            try:
                await asyncio.wait_for(asyncio.shield(completion), OUTPUT_DRAIN_TIMEOUT_SECS)
            except TimeoutError:
                completion.cancel()
        raise
    finally:
        cancellation_task.cancel()
        if process.returncode is None:
            _kill_process(process)
        for task in (stdout_task, stderr_task, completion):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, completion, return_exceptions=True)
        if process.returncode is None:
            with suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), OUTPUT_DRAIN_TIMEOUT_SECS)


async def _capture_output(
    stream: asyncio.StreamReader | None,
) -> tuple[str, bool]:
    if stream is None:
        return "", False
    data = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = MAX_SHELL_OUTPUT_BYTES - len(data)
        if remaining > 0:
            data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return data.decode("utf-8", errors="replace"), truncated


def _kill_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform != "win32" and os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            with suppress(ProcessLookupError):
                process.kill()
    else:
        with suppress(ProcessLookupError):
            process.kill()


def _task_output(task: asyncio.Task[tuple[str, bool]]) -> tuple[str, bool]:
    if task.done() and not task.cancelled():
        try:
            return task.result()
        except Exception:
            return "", True
    return "", True


def _parse_ripgrep_match(frame: str) -> tuple[str, int, str] | None:
    if not frame.strip():
        return None
    try:
        event = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse ripgrep JSON output: {exc}") from exc
    if event.get("type") != "match":
        return None
    data = event.get("data") or {}
    path = (data.get("path") or {}).get("text")
    text = (data.get("lines") or {}).get("text")
    line = data.get("line_number")
    if not isinstance(path, str) or not isinstance(text, str) or not _is_int(line):
        return None
    return path, line, text


def _read_utf8(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _text_lines(content: str) -> list[str]:
    if not content:
        return []
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _validate_approval(
    approval: ToolApproval,
    required: ApprovalRequest,
    *,
    stale_error: str,
) -> str | None:
    decision: ApprovalDecision = approval.decision
    original: ApprovalRequest = approval.request
    if decision.request_id != original.id:
        return (
            f"approval decision {decision.request_id} does not match pending approval {original.id}"
        )
    if original.id != required.id:
        return f"approval request {original.id} does not match required approval {required.id}"
    if original.action != required.action:
        return stale_error
    return None


def _parse_args(call: ToolCall, required: set[str], optional: set[str]) -> dict[str, Any]:
    try:
        value = json.loads(call.function.arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid arguments for tool {call.function.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid arguments for tool {call.function.name}: expected object")
    missing = required - value.keys()
    if missing:
        name = sorted(missing)[0]
        raise ValueError(f"invalid arguments for tool {call.function.name}: missing field {name!r}")
    unknown = value.keys() - required - optional
    if unknown:
        name = sorted(unknown)[0]
        raise ValueError(f"invalid arguments for tool {call.function.name}: unknown field {name!r}")
    return value


def _require_str(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_str(args: dict[str, Any], name: str, default: str) -> str:
    if name not in args or args[name] is None:
        return default
    return _require_str(args, name)


def _optional_bool(args: dict[str, Any], name: str, default: bool) -> bool:
    if name not in args or args[name] is None:
        return default
    value = args[name]
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _optional_int(args: dict[str, Any], name: str, default: int) -> int:
    if name not in args or args[name] is None:
        return default
    value = args[name]
    if not _is_int(value):
        raise TypeError(f"{name} must be an integer")
    return value


def _clamp_limit(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not _is_int(value):
        raise TypeError("limit must be an integer")
    limited = min(int(value), maximum)
    if limited < 1:
        raise ValueError("limit must be at least 1")
    return limited


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _tool_ok(data: dict[str, Any], summary: ToolExecutionSummary | None = None) -> ToolResult:
    return ToolResult(
        ok=True,
        content=_json_dumps({"ok": True, "data": data}),
        error=None,
        summary=summary,
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_cancelled(cancellation: Any) -> bool:
    value = getattr(cancellation, "is_cancelled", False)
    return bool(value()) if callable(value) else bool(value)


async def _wait_cancelled(cancellation: Any) -> None:
    method = getattr(cancellation, "cancelled", None)
    if callable(method):
        await method()
    else:  # pragma: no cover - defensive compatibility path
        await asyncio.Future()


__all__ = [
    "BuiltInTools",
    "TOOL_CANCELLED_ERROR",
    "built_in_definitions",
    "run_shell_command",
]

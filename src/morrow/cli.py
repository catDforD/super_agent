from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from morrow.config import ConfigError, ContextConfig, LoadedConfig, load_config
from morrow.model import ModelError, OpenAiCompatClient, OpenAiCompatConfig
from morrow.protocol import (
    AgentEventType,
    ApprovalActionKind,
    ApprovalDecision,
    ApprovalRequest,
    FileChangeSummary,
    PermissionMode,
    PermissionProfile,
    Session,
    ShellCommandSummary,
    ShellPolicy,
    ToolCallFinishedData,
    ToolExecutionSummary,
)
from morrow.runtime.agent import (
    RunAgentTurnContext,
    RunAgentTurnOutcome,
    TurnEventHandler,
    run_agent_turn,
)
from morrow.runtime.agent import (
    RuntimeError as AgentRuntimeError,
)
from morrow.runtime.compaction import CompactionOutcome, compact_session
from morrow.runtime.events import AgentEventEnvelope
from morrow.runtime.session_store import SessionStore, SessionStoreError
from morrow.server.app import ServerOptions, serve
from morrow.tools.mcp import McpToolCache
from morrow.workspace import detect_workspace_root

INIT_CONFIG_MODEL = "gpt-4.1"
INIT_CONFIG_BASE_URL = "https://api.openai.com/v1"
INIT_CONFIG_API_KEY_PLACEHOLDER = "replace-with-your-openai-api-key"
INIT_CONFIG_TIMEOUT_SECS = 120
INIT_CONFIG_CONTEXT_WINDOW_TOKENS = 1_047_576
INIT_CONFIG_RESERVED_OUTPUT_TOKENS = 8_192


class CliError(RuntimeError):
    pass


@dataclass(slots=True)
class CliArgs:
    config: Path | None = None
    session: str | None = None
    thread: str | None = None
    reset_session: bool = False
    reset_thread: bool = False
    permission: PermissionMode | None = None
    allow_shell: bool = False
    jsonl: bool = False
    command: str | None = None
    command_args: argparse.Namespace | None = None
    prompt: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionRecord:
    name: str
    ok: bool
    summary: ToolExecutionSummary | None


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


async def run(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        await _run(args)
        return 0
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (ConfigError, ModelError, AgentRuntimeError, SessionStoreError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def parse_args(argv: list[str]) -> CliArgs:
    global_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    global_parser.add_argument("--config", type=Path)
    global_parser.add_argument("--session")
    global_parser.add_argument("--thread")
    global_parser.add_argument("--reset-session", action="store_true")
    global_parser.add_argument("--reset-thread", action="store_true")
    global_parser.add_argument("--permission", type=_parse_permission_mode)
    global_parser.add_argument("--allow-shell", action="store_true")
    global_parser.add_argument("--jsonl", action="store_true")
    known, remaining = global_parser.parse_known_args(argv)

    if known.session is not None and known.thread is not None:
        raise CliError("--session and --thread cannot be used together")

    result = CliArgs(
        config=known.config,
        session=known.session,
        thread=known.thread,
        reset_session=known.reset_session,
        reset_thread=known.reset_thread,
        permission=known.permission,
        allow_shell=known.allow_shell,
        jsonl=known.jsonl,
    )
    if remaining and remaining[0] in {"init", "server", "session"}:
        result.command = remaining[0]
        result.command_args = _parse_command(remaining[0], remaining[1:])
    elif "-h" in remaining or "--help" in remaining:
        _main_help_parser().print_help()
        raise SystemExit(0)
    else:
        result.prompt = [value for value in remaining if value != "--"]
    return result


def _main_help_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="morrow",
        description="Local OpenAI-compatible coding agent CLI and web dashboard.",
    )
    parser.add_argument("--config", metavar="PATH")
    parser.add_argument("--session", metavar="NAME")
    parser.add_argument("--thread", metavar="NAME", help="deprecated alias for --session")
    parser.add_argument("--reset-session", action="store_true")
    parser.add_argument("--reset-thread", action="store_true")
    parser.add_argument("--permission", metavar="MODE")
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument("command_or_prompt", nargs="*")
    return parser


def _parse_command(command: str, argv: list[str]) -> argparse.Namespace:
    if command == "init":
        parser = argparse.ArgumentParser(prog="morrow init")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--template", action="store_true")
        return parser.parse_args(argv)
    if command == "server":
        parser = argparse.ArgumentParser(prog="morrow server")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=3000)
        namespace = parser.parse_args(argv)
        if not 0 <= namespace.port <= 65_535:
            parser.error("--port must be between 0 and 65535")
        return namespace

    parser = argparse.ArgumentParser(prog="morrow session")
    commands = parser.add_subparsers(dest="session_command", required=True)
    commands.add_parser("list")
    show = commands.add_parser("show")
    show.add_argument("name", nargs="?")
    delete = commands.add_parser("delete")
    delete.add_argument("name")
    rename = commands.add_parser("rename")
    rename.add_argument("old")
    rename.add_argument("new")
    export = commands.add_parser("export")
    export.add_argument("name", nargs="?")
    export.add_argument("--output", type=Path)
    return parser.parse_args(argv)


async def _run(args: CliArgs) -> None:
    session_name = args.session or args.thread or "default"
    if args.command is not None and args.jsonl:
        raise CliError("--jsonl cannot be used with commands")
    if args.command == "init":
        assert args.command_args is not None
        _handle_init(args.command_args.force, args.command_args.template)
        return
    if args.command == "session":
        assert args.command_args is not None
        _handle_session_command(args.command_args, session_name)
        return

    prompt = " ".join(args.prompt)
    if args.jsonl and not prompt.strip():
        raise CliError("--jsonl requires a prompt and cannot be used in interactive mode")

    loaded = _load_config(args.config)
    permissions = _effective_permissions(
        loaded.config.permissions, args.permission, args.allow_shell
    )
    client = OpenAiCompatClient.new(
        OpenAiCompatConfig(
            base_url=loaded.config.model.base_url,
            model=loaded.config.model.model,
            api_key=loaded.api_key,
            timeout=float(loaded.config.model.timeout_secs),
        )
    )
    mcp_cache = McpToolCache()
    try:
        workspace_root = detect_workspace_root()
        if args.command == "server":
            assert args.command_args is not None
            print(
                f"morrow server listening on http://{args.command_args.host}:"
                f"{args.command_args.port}",
                file=sys.stderr,
            )
            await serve(
                ServerOptions(
                    host=args.command_args.host,
                    port=args.command_args.port,
                    client=client,
                    system_prompt=loaded.config.agent.system_prompt,
                    context_config=loaded.config.context,
                    model_limits=loaded.config.model.context_limits(),
                    workspace_root=workspace_root,
                    config_path=loaded.path,
                    permissions=permissions,
                    mcp_servers=loaded.config.mcp_servers,
                    plc_subagents=loaded.config.plc_subagents,
                    default_session_name=session_name,
                )
            )
            return

        store = SessionStore.for_current_dir(session_name)
        session = Session.new() if args.reset_session or args.reset_thread else store.load()
        if not prompt.strip():
            await _run_repl(
                loaded,
                client,
                mcp_cache,
                store,
                session_name,
                workspace_root,
                session,
                permissions,
            )
            return

        outcome = await _run_agent_turn(
            loaded,
            client,
            mcp_cache,
            session_name,
            workspace_root,
            permissions,
            session,
            prompt,
            jsonl=args.jsonl,
        )
        if outcome.session_changed:
            store.save(session)
        if outcome.error:
            raise CliError(f"agent run failed: {outcome.error}")
    finally:
        await client.aclose()
        await _maybe_aclose(mcp_cache)


def _load_config(path: Path | None) -> LoadedConfig:
    try:
        return load_config(path)
    except Exception as error:
        raise CliError(str(error)) from error


async def _run_repl(
    loaded: LoadedConfig,
    client: OpenAiCompatClient,
    mcp_cache: McpToolCache,
    store: SessionStore,
    session_name: str,
    workspace_root: Path,
    session: Session,
    permissions: PermissionProfile,
) -> None:
    print("morrow interactive mode. Type /exit to quit.", file=sys.stderr)
    active_permissions = permissions.model_copy(deep=True)
    while True:
        try:
            text = await asyncio.to_thread(input, "morrow> ")
        except EOFError:
            return
        text = text.strip()
        if not text:
            continue
        if text.startswith("/"):
            should_exit = await _handle_repl_command(
                text,
                loaded,
                client,
                store,
                session_name,
                workspace_root,
                session,
                active_permissions,
            )
            if should_exit:
                return
            continue

        outcome = await _run_agent_turn(
            loaded,
            client,
            mcp_cache,
            session_name,
            workspace_root,
            active_permissions,
            session,
            text,
            jsonl=False,
        )
        if outcome.session_changed:
            store.save(session)
        if outcome.error:
            raise CliError(f"agent run failed: {outcome.error}")


async def _handle_repl_command(
    text: str,
    loaded: LoadedConfig,
    client: OpenAiCompatClient,
    store: SessionStore,
    session_name: str,
    workspace_root: Path,
    session: Session,
    permissions: PermissionProfile,
) -> bool:
    parts = text.split()
    command = parts[0]
    if command in {"/exit", "/quit"}:
        return True
    if command == "/status":
        print(f"session: {session_name}", file=sys.stderr)
        print(f"turns: {len(session.turns)}", file=sys.stderr)
        print(f"active messages: {len(session.active_thread.messages)}", file=sys.stderr)
        print(f"summary: {'yes' if session.context.summary else 'no'}", file=sys.stderr)
        print(f"workspace: {workspace_root}", file=sys.stderr)
        print(f"config: {loaded.path}", file=sys.stderr)
        print(f"context: {_context_summary(loaded.config.context)}", file=sys.stderr)
        print(f"permissions: {_permission_summary(permissions)}", file=sys.stderr)
        return False
    if command == "/reset":
        reset = Session.new()
        session.active_thread = reset.active_thread
        session.turns = reset.turns
        session.context = reset.context
        store.save(session)
        print("session reset", file=sys.stderr)
        return False
    if command == "/compact":
        outcome = await compact_session(client, session, loaded.config.context)
        if outcome is CompactionOutcome.CHANGED:
            store.save(session)
            print("session compacted", file=sys.stderr)
        else:
            print("no compactable session history", file=sys.stderr)
        return False
    if command == "/permissions":
        if len(parts) == 1:
            print(f"permissions: {_permission_summary(permissions)}", file=sys.stderr)
            return False
        try:
            new_profile = PermissionProfile.for_mode(_parse_permission_mode(parts[1]))
        except argparse.ArgumentTypeError as error:
            print(error, file=sys.stderr)
            return False
        permissions.mode = new_profile.mode
        permissions.shell = new_profile.shell
        print(f"permissions: {_permission_summary(permissions)}", file=sys.stderr)
        return False
    print(f"unknown command: {command}", file=sys.stderr)
    return False


async def _run_agent_turn(
    loaded: LoadedConfig,
    client: OpenAiCompatClient,
    mcp_cache: McpToolCache,
    session_name: str,
    workspace_root: Path,
    permissions: PermissionProfile,
    session: Session,
    prompt: str,
    *,
    jsonl: bool,
) -> RunAgentTurnOutcome:
    handler = CliTurnHandler(
        permissions=permissions,
        interactive=sys.stdin.isatty(),
        jsonl=jsonl,
    )
    try:
        return await run_agent_turn(
            RunAgentTurnContext(
                client=client,
                system_prompt=loaded.config.agent.system_prompt,
                context_config=loaded.config.context,
                model_limits=loaded.config.model.context_limits(),
                workspace_root=workspace_root,
                permissions=permissions,
                mcp_servers=loaded.config.mcp_servers,
                plc_subagents=loaded.config.plc_subagents,
                mcp_cache=mcp_cache,
                session_name=session_name,
                turn_index=len(session.turns),
            ),
            session,
            prompt,
            handler,
        )
    except AgentRuntimeError as error:
        raise CliError(str(error)) from error


class CliTurnHandler(TurnEventHandler):
    def __init__(
        self,
        *,
        permissions: PermissionProfile,
        interactive: bool,
        jsonl: bool,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> None:
        self.permissions = permissions
        self.interactive = interactive
        self.jsonl = jsonl
        self.stdout = stdout
        self.stderr = stderr
        self.wrote_text = False
        self.output_ends_with_newline = False
        self.execution_records: list[ExecutionRecord] = []

    def on_event(self, envelope: AgentEventEnvelope) -> None:
        if self.jsonl:
            self.stdout.write(
                json.dumps(envelope.to_wire(), ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self.stdout.flush()

        event = envelope.event
        if event.type is AgentEventType.WARNING and not self.jsonl:
            print(f"warning: {event.data}", file=self.stderr)
        elif event.type is AgentEventType.TEXT_DELTA and not self.jsonl:
            text = str(event.data)
            self.wrote_text = True
            self.output_ends_with_newline = text.endswith("\n")
            self.stdout.write(text)
            self.stdout.flush()
        elif event.type is AgentEventType.TOOL_CALL_STARTED and not self.jsonl:
            print(f"tool {event.data.name} started", file=self.stderr)
        elif event.type is AgentEventType.TOOL_CALL_FINISHED:
            data = event.data
            assert isinstance(data, ToolCallFinishedData)
            if not self.jsonl:
                print(f"tool {data.name} {'ok' if data.ok else 'error'}", file=self.stderr)
            self.execution_records.append(ExecutionRecord(data.name, data.ok, data.summary))
        elif event.type is AgentEventType.APPROVAL_RESOLVED and not self.jsonl:
            decision = event.data
            print(
                f"approval {decision.request_id} {'approved' if decision.approved else 'denied'}",
                file=self.stderr,
            )
        elif event.type is AgentEventType.TURN_COMPLETED and not self.jsonl:
            if self.wrote_text and not self.output_ends_with_newline:
                self.stdout.write("\n")
                self.stdout.flush()
            summary = _format_execution_summary(self.execution_records)
            if summary:
                self.stderr.write(summary)

    async def resolve_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.stderr.write(_format_approval_request(request, self.permissions))
        self.stderr.flush()
        if not self.interactive:
            print("stdin is not interactive; approval denied by default", file=self.stderr)
            return ApprovalDecision.deny(request.id)
        answer = await asyncio.to_thread(input, "approve this action? [y/N] ")
        return (
            ApprovalDecision.approve(request.id)
            if answer.strip().lower() in {"y", "yes"}
            else ApprovalDecision.deny(request.id)
        )


def _handle_init(force: bool, template: bool) -> None:
    path = Path.home() / ".morrow" / "config.toml"
    if path.exists() and not force:
        raise CliError(f"config file already exists: {path}; use --force to overwrite it")
    api_key = INIT_CONFIG_API_KEY_PLACEHOLDER if template else getpass.getpass("OpenAI API key: ")
    if not api_key.strip():
        raise CliError("API key must not be empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_init_config(api_key), encoding="utf-8")
    print(f"wrote config: {path}")
    if template:
        print("edit [model].OPENAI_API_KEY before running morrow")
    else:
        print('try: morrow "hello"')


def _render_init_config(api_key: str) -> str:
    escaped = api_key.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "[model]\n"
        f'base_url = "{INIT_CONFIG_BASE_URL}"\n'
        f'model = "{INIT_CONFIG_MODEL}"\n'
        f'OPENAI_API_KEY = "{escaped}"\n'
        f"timeout_secs = {INIT_CONFIG_TIMEOUT_SECS}\n"
        f"context_window_tokens = {INIT_CONFIG_CONTEXT_WINDOW_TOKENS}\n"
        f"reserved_output_tokens = {INIT_CONFIG_RESERVED_OUTPUT_TOKENS}\n\n"
        "[permissions]\n"
        'mode = "read_only"\n'
        'shell = "deny"\n'
    )


def _handle_session_command(args: argparse.Namespace, default_name: str) -> None:
    command = args.session_command
    if command == "list":
        entries = SessionStore.for_current_dir(default_name).list_current_scope()
        if not entries:
            print("no sessions")
            return
        print("NAME\tTURNS\tACTIVE_MESSAGES\tSUMMARY\tPATH")
        for entry in entries:
            print(
                f"{entry.name}\t{entry.turns}\t{entry.active_messages}\t"
                f"{'yes' if entry.has_summary else 'no'}\t{entry.path}"
            )
        return

    name = getattr(args, "name", None) or default_name
    store = SessionStore.for_current_dir(name)
    if command == "show":
        session = store.load_existing()
        print(f"name: {name}")
        print(f"path: {store.path}")
        print(f"turns: {len(session.turns)}")
        print(f"active_messages: {len(session.active_thread.messages)}")
        print(f"summarized_turns: {session.context.summarized_turns}")
        print(f"summary: {'yes' if session.context.summary else 'no'}")
    elif command == "delete":
        store.delete()
        print(f"deleted session: {name}")
    elif command == "rename":
        source = SessionStore.for_current_dir(args.old)
        target = source.rename(args.new)
        print(f"renamed session: {args.old} -> {args.new} ({target.path})")
    elif command == "export":
        data = store.export_document_bytes()
        if args.output is None:
            sys.stdout.buffer.write(data + b"\n")
            sys.stdout.buffer.flush()
        else:
            if args.output.exists():
                raise CliError(f"output file already exists: {args.output}")
            args.output.write_bytes(data)
            print(f"exported session: {name} -> {args.output}", file=sys.stderr)


def _effective_permissions(
    base: PermissionProfile,
    mode_override: PermissionMode | None,
    allow_shell: bool,
) -> PermissionProfile:
    profile = (
        PermissionProfile.for_mode(mode_override)
        if mode_override is not None
        else base.model_copy(deep=True)
    )
    if allow_shell:
        profile.shell = ShellPolicy.ALLOW
    return profile


def _parse_permission_mode(value: str) -> PermissionMode:
    normalized = value.replace("-", "_")
    try:
        return PermissionMode(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid permission mode {value!r}; expected read-only, workspace-write, "
            "or danger-full-access"
        ) from error


def _permission_summary(permissions: PermissionProfile) -> str:
    return f"mode={permissions.mode.value}, shell={permissions.shell.value}"


def _context_summary(context: ContextConfig) -> str:
    return (
        f"auto_compact={str(context.auto_compact).lower()}, "
        f"auto_compact_threshold={context.auto_compact_threshold}, "
        f"retain_recent_turns={context.retain_recent_turns}, "
        f"summary_target_tokens={context.summary_target_tokens}, "
        f"compact_max_retries={context.compact_max_retries}"
    )


def _format_approval_request(request: ApprovalRequest, permissions: PermissionProfile) -> str:
    output = [f"approval required: {request.reason}\n"]
    action = request.action
    if action.kind is ApprovalActionKind.SHELL_COMMAND:
        output.extend(
            [
                "action: shell command\n",
                f"command: {action.command}\n",
                f"cwd: {action.cwd}\n",
                f"timeout: {action.timeout_secs}s\n",
                f"permissions: {_permission_summary(permissions)}\n",
                "warning: approving this command may modify files or access the network.\n",
            ]
        )
    else:
        output.append("action: file changes\n")
        output.append(_format_file_list(action.files or []))
        output.append("diff:\n")
        output.append(action.diff or "")
        if not (action.diff or "").endswith("\n"):
            output.append("\n")
        output.append(f"permissions: {_permission_summary(permissions)}\n")
        output.append("warning: approving this action will modify files.\n")
    return "".join(output)


def _format_execution_summary(records: list[ExecutionRecord]) -> str | None:
    if not records:
        return None
    output = ["execution summary:\n"]
    for record in records:
        output.append(f"- {record.name}: {'ok' if record.ok else 'error'}\n")
        summary = record.summary
        if summary is None:
            continue
        if summary.files:
            output.append(_format_file_list(summary.files))
            if summary.diff:
                output.append("  diff: available\n")
        if summary.shell_summary is not None:
            output.append(_format_shell_summary(summary.shell_summary))
        if summary.error_message:
            output.append(f"  error: {summary.error_message}\n")
    return "".join(output)


def _format_file_list(files: list[FileChangeSummary]) -> str:
    if not files:
        return "files: none\n"
    output = ["files:\n"]
    for item in files:
        output.append(
            f"- {item.path} ({item.operation.value}, replacements={item.replacements}, "
            f"created={str(item.created).lower()}, "
            f"overwritten={str(item.overwritten).lower()}, "
            f"deleted={str(item.deleted).lower()})\n"
        )
    return "".join(output)


def _format_shell_summary(shell: ShellCommandSummary) -> str:
    exit_code = "none" if shell.exit_code is None else str(shell.exit_code)
    return (
        f"  shell: exit_code={exit_code}, timed_out={str(shell.timed_out).lower()}, "
        f"stdout_truncated={str(shell.stdout_truncated).lower()}, "
        f"stderr_truncated={str(shell.stderr_truncated).lower()}\n"
    )


async def _maybe_aclose(value: object) -> None:
    close = getattr(value, "aclose", None) or getattr(value, "close", None)
    if close is None:
        return
    result = close()
    if asyncio.iscoroutine(result):
        with contextlib.suppress(Exception):
            await result


__all__ = ["CliArgs", "CliError", "CliTurnHandler", "main", "parse_args", "run"]

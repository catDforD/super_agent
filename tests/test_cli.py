from __future__ import annotations

import argparse
import io
import json
import tomllib
from pathlib import Path

import pytest

import morrow.cli as cli
from morrow.protocol import (
    AgentEvent,
    Message,
    PermissionMode,
    PermissionProfile,
    Session,
    ShellPolicy,
    Thread,
)
from morrow.runtime.events import AgentEventEnvelope
from morrow.runtime.session_store import SessionStore


def test_two_stage_parser_accepts_globals_around_subcommands() -> None:
    before = cli.parse_args(["--session", "work", "server", "--port", "4123"])
    after = cli.parse_args(["server", "--port", "4123", "--session", "work"])

    for parsed in (before, after):
        assert parsed.session == "work"
        assert parsed.command == "server"
        assert parsed.command_args is not None
        assert parsed.command_args.host == "127.0.0.1"
        assert parsed.command_args.port == 4123

    export = cli.parse_args(["--thread", "legacy", "session", "export", "--output", "session.json"])
    assert export.thread == "legacy"
    assert export.command == "session"
    assert export.command_args is not None
    assert export.command_args.session_command == "export"
    assert export.command_args.output == Path("session.json")


def test_parser_distinguishes_escaped_prompt_and_rejects_alias_conflict() -> None:
    prompt = cli.parse_args(["--jsonl", "--", "session"])
    assert prompt.command is None
    assert prompt.prompt == ["session"]

    with pytest.raises(cli.CliError, match="cannot be used together"):
        cli.parse_args(["--session", "work", "--thread", "legacy"])


def test_permission_modes_and_overrides_preserve_base_profile() -> None:
    assert cli._parse_permission_mode("read-only") is PermissionMode.READ_ONLY
    assert cli._parse_permission_mode("workspace_write") is PermissionMode.WORKSPACE_WRITE
    assert cli._parse_permission_mode("danger-full-access") is PermissionMode.DANGER_FULL_ACCESS
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_permission_mode("full")

    base = PermissionProfile(mode=PermissionMode.WORKSPACE_WRITE, shell=ShellPolicy.DENY)
    unchanged = cli._effective_permissions(base, None, False)
    dangerous = cli._effective_permissions(base, PermissionMode.DANGER_FULL_ACCESS, False)
    shell_allowed = cli._effective_permissions(base, None, True)

    assert unchanged == base
    assert unchanged is not base
    assert dangerous == PermissionProfile.for_mode(PermissionMode.DANGER_FULL_ACCESS)
    assert shell_allowed == PermissionProfile(
        mode=PermissionMode.WORKSPACE_WRITE,
        shell=ShellPolicy.ALLOW,
    )
    assert base.shell is ShellPolicy.DENY


def test_init_template_is_valid_toml_and_refuses_unforced_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    cli._handle_init(force=False, template=True)

    path = home / ".morrow" / "config.toml"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["model"]["base_url"] == cli.INIT_CONFIG_BASE_URL
    assert parsed["model"]["model"] == cli.INIT_CONFIG_MODEL
    assert parsed["model"]["OPENAI_API_KEY"] == cli.INIT_CONFIG_API_KEY_PLACEHOLDER
    assert parsed["model"]["context_window_tokens"] == cli.INIT_CONFIG_CONTEXT_WINDOW_TOKENS
    assert parsed["permissions"] == {"mode": "read_only", "shell": "deny"}
    assert "edit [model].OPENAI_API_KEY" in capsys.readouterr().out

    with pytest.raises(cli.CliError, match="already exists"):
        cli._handle_init(force=False, template=True)

    cli._handle_init(force=True, template=True)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["model"]["model"] == "gpt-4.1"


def test_init_renderer_escapes_inline_api_key() -> None:
    rendered = cli._render_init_config('key\\with"quotes')
    parsed = tomllib.loads(rendered)
    assert parsed["model"]["OPENAI_API_KEY"] == 'key\\with"quotes'


@pytest.mark.anyio
async def test_jsonl_validation_fails_before_loading_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await cli.run(["--jsonl"]) == 1
    assert "--jsonl requires a prompt" in capsys.readouterr().err

    assert await cli.run(["--jsonl", "session", "list"]) == 1
    assert "--jsonl cannot be used with commands" in capsys.readouterr().err


def test_jsonl_turn_handler_emits_only_event_envelopes() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    handler = cli.CliTurnHandler(
        permissions=PermissionProfile.for_mode(PermissionMode.READ_ONLY),
        interactive=False,
        jsonl=True,
        stdout=stdout,
        stderr=stderr,
    )
    events = [
        AgentEvent.turn_started(),
        AgentEvent.text_delta("hello"),
        AgentEvent.agent_message("hello"),
        AgentEvent.turn_completed(),
    ]
    for index, event in enumerate(events):
        handler.on_event(
            AgentEventEnvelope(
                schema_version=1,
                timestamp_ms=1234 + index,
                session="default",
                workspace_root="/workspace",
                turn_index=2,
                event_index=index,
                event=event,
            )
        )

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["event"]["type"] for line in lines] == [
        "turn_started",
        "text_delta",
        "agent_message",
        "turn_completed",
    ]
    assert lines[0] == {
        "schema_version": 1,
        "timestamp_ms": 1234,
        "session": "default",
        "workspace_root": "/workspace",
        "turn_index": 2,
        "event_index": 0,
        "event": {"type": "turn_started"},
    }
    assert lines[1]["event"] == {"type": "text_delta", "data": "hello"}
    assert stderr.getvalue() == ""
    assert "execution summary:" not in stdout.getvalue()


def test_session_subcommands_operate_in_current_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    session = Session.from_thread(Thread(messages=[Message.user("Hello"), Message.assistant("Hi")]))
    SessionStore.for_current_dir("work").save(session)
    SessionStore.for_current_dir("default").save(Session.new())

    list_args = cli.parse_args(["session", "list"]).command_args
    assert list_args is not None
    cli._handle_session_command(list_args, "default")
    listing = capsys.readouterr().out
    assert "NAME\tTURNS\tACTIVE_MESSAGES\tSUMMARY\tPATH" in listing
    assert "default\t0\t0\tno" in listing
    assert "work\t0\t2\tno" in listing

    show_args = cli.parse_args(["session", "show", "work"]).command_args
    assert show_args is not None
    cli._handle_session_command(show_args, "default")
    shown = capsys.readouterr().out
    assert "name: work" in shown
    assert "active_messages: 2" in shown

    rename_args = cli.parse_args(["session", "rename", "work", "renamed"]).command_args
    assert rename_args is not None
    cli._handle_session_command(rename_args, "default")
    assert SessionStore.for_current_dir("renamed").load_existing() == session
    assert not SessionStore.for_current_dir("work").path.exists()
    capsys.readouterr()

    output = tmp_path / "export.json"
    export_args = cli.parse_args(
        ["session", "export", "renamed", "--output", str(output)]
    ).command_args
    assert export_args is not None
    cli._handle_session_command(export_args, "default")
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["schema_version"] == 3
    assert exported["session"]["active_thread"]["messages"][0]["content"] == "Hello"
    assert "exported session: renamed" in capsys.readouterr().err

    delete_args = cli.parse_args(["session", "delete", "renamed"]).command_args
    assert delete_args is not None
    cli._handle_session_command(delete_args, "default")
    assert not SessionStore.for_current_dir("renamed").path.exists()

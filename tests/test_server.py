from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from morrow.config import ContextConfig, ModelContextLimits
from morrow.core import ModelEvent, ModelRequest
from morrow.protocol import PermissionMode, PermissionProfile
from morrow.server.app import ServerOptions, _handle_client_message, create_app


class TextModel:
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request

        async def events() -> AsyncIterator[ModelEvent]:
            yield ModelEvent.text_delta("hello")
            yield ModelEvent.completed()

        return events()


def options(tmp_path: Path) -> ServerOptions:
    return ServerOptions(
        host="127.0.0.1",
        port=3000,
        client=TextModel(),
        system_prompt="You are helpful.",
        context_config=ContextConfig(
            auto_compact=True,
            auto_compact_threshold=0.835,
            retain_recent_turns=6,
            summary_target_tokens=12_000,
            compact_max_retries=2,
        ),
        model_limits=ModelContextLimits(128_000, 8_192),
        workspace_root=tmp_path,
        config_path=tmp_path / "morrow.toml",
        permissions=PermissionProfile.for_mode(PermissionMode.READ_ONLY),
        mcp_servers=[],
    )


@pytest.mark.anyio
async def test_status_and_session_crud_use_frontend_contract(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    app = create_app(options(workspace))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["workspace_root"] == str(workspace)
        assert status.json()["workspace_selection_enabled"] is True
        assert "api_key" not in status.text

        created = await client.post("/api/sessions/work")
        assert created.status_code == 200
        assert created.json()["schema_version"] == 3
        assert (await client.post("/api/sessions/work")).status_code == 409

        listing = (await client.get("/api/sessions")).json()
        assert [entry["name"] for entry in listing] == ["work"]

        reset = await client.post("/api/sessions/work/reset")
        assert reset.status_code == 200
        assert reset.json()["session"]["turns"] == []


@pytest.mark.anyio
async def test_websocket_runs_and_saves_turn(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    app = create_app(options(workspace))
    state = app.state.morrow
    runtime = await state.runtime_for("default")
    queue = runtime.subscribe()
    await _handle_client_message(
        state,
        runtime,
        "default",
        {
            "type": "start_turn",
            "data": {"request_id": "request-1", "prompt": "hi"},
        },
    )
    message_types: list[str] = []
    event_types: list[str] = []
    while "turn_saved" not in message_types:
        message = await asyncio.wait_for(queue.get(), timeout=2)
        message_types.append(message["type"])
        if message["type"] == "agent_event":
            event_types.append(message["data"]["event"]["type"])

    assert "turn_started" in event_types
    assert "text_delta" in event_types
    assert "turn_completed" in event_types
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        document = (await client.get("/api/sessions/default")).json()
    assert document["session"]["turns"][0]["turn"]["status"] == "completed"
    assert document["session"]["turns"][0]["turn"]["assistant_message"]["content"] == "hello"
    runtime.unsubscribe(queue)
    await state.close()


@pytest.mark.anyio
async def test_directory_browser_filters_hidden_entries_and_sorts_directories_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "z-folder").mkdir()
    (home / "a-folder").mkdir()
    (home / ".hidden-folder").mkdir()
    (home / "file.txt").write_text("file", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    app = create_app(options(workspace))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get("/api/workspaces/directory")
        hidden = await client.get(
            "/api/workspaces/directory",
            params={"path": str(home), "show_hidden": "true"},
        )

    assert listing.status_code == 200
    assert listing.json()["path"] == str(home)
    assert listing.json()["parent"] == str(tmp_path)
    assert [entry["name"] for entry in listing.json()["entries"]] == [
        "a-folder",
        "z-folder",
        "file.txt",
    ]
    assert [entry["name"] for entry in hidden.json()["entries"]] == [
        ".hidden-folder",
        "a-folder",
        "z-folder",
        "file.txt",
    ]


@pytest.mark.anyio
async def test_workspace_switch_isolates_sessions_and_updates_turn_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    home.mkdir()
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("HOME", str(home))

    app = create_app(options(first))
    state = app.state.morrow
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/api/sessions/work")).status_code == 200
        switched = await client.post("/api/workspaces/open", json={"path": str(second)})
        assert switched.status_code == 200
        assert switched.json()["workspace_root"] == str(second)
        assert (await client.get("/api/sessions")).json() == []

        runtime = await state.runtime_for("default")
        queue = runtime.subscribe()
        await _handle_client_message(
            state,
            runtime,
            "default",
            {
                "type": "start_turn",
                "data": {"request_id": "request-second", "prompt": "hi"},
            },
        )
        event_workspace = None
        while True:
            message = await asyncio.wait_for(queue.get(), timeout=2)
            if message["type"] == "agent_event":
                event_workspace = message["data"]["workspace_root"]
            if message["type"] == "turn_saved":
                break
        runtime.unsubscribe(queue)
        assert event_workspace == str(second)

        switched_back = await client.post("/api/workspaces/open", json={"path": str(first)})
        assert switched_back.status_code == 200
        listing = await client.get("/api/sessions")
        assert [entry["name"] for entry in listing.json()] == ["work"]

    await state.close()

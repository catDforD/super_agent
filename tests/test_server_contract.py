from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect

import morrow.server.app as server
from morrow.config import ContextConfig, ModelContextLimits
from morrow.core import CancellationToken, ModelEvent, ModelRequest
from morrow.protocol import ApprovalDecision, PermissionMode, PermissionProfile


class TextModel:
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request

        async def events() -> AsyncIterator[ModelEvent]:
            yield ModelEvent.text_delta("hello")
            yield ModelEvent.completed()

        return events()


def _options(workspace: Path) -> server.ServerOptions:
    return server.ServerOptions(
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
        workspace_root=workspace,
        config_path=workspace / "morrow.toml",
        permissions=PermissionProfile.for_mode(PermissionMode.READ_ONLY),
        mcp_servers=[],
    )


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    return home, workspace


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def _websocket(
    *, origin: str | None, host: str = "127.0.0.1:3000", scheme: str = "ws"
) -> WebSocket:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return WebSocket(
        {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": scheme,
            "path": "/api/sessions/default/ws",
            "raw_path": b"/api/sessions/default/ws",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 3000),
            "subprotocols": [],
        },
        receive=lambda: _wait_forever(),
        send=lambda _message: _wait_forever(),
    )


def test_websocket_origin_policy_blocks_cross_site_browser_connections() -> None:
    assert server._websocket_origin_allowed(_websocket(origin=None))
    assert server._websocket_origin_allowed(_websocket(origin="http://127.0.0.1:3000"))
    assert server._websocket_origin_allowed(
        _websocket(origin="https://example.test", host="example.test", scheme="wss")
    )
    assert not server._websocket_origin_allowed(_websocket(origin="https://evil.example"))
    assert not server._websocket_origin_allowed(_websocket(origin="null"))
    assert not server._websocket_origin_allowed(_websocket(origin="http://127.0.0.1:3001"))


@pytest.mark.anyio
async def test_static_assets_and_rest_errors_follow_frontend_contract(
    isolated_workspace: tuple[Path, Path],
) -> None:
    _, workspace = isolated_workspace
    app = server.create_app(_options(workspace))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        index = await client.get("/")
        javascript = await client.get("/assets/app.js")
        stylesheet = await client.get("/assets/style.css")
        missing_asset = await client.get("/assets/missing.js")

        assert index.status_code == 200
        assert "<!doctype html>" in index.text.lower()
        assert javascript.status_code == 200
        assert javascript.headers["content-type"].startswith("application/javascript")
        assert stylesheet.status_code == 200
        assert stylesheet.headers["content-type"].startswith("text/css")
        assert missing_asset.status_code == 404

        invalid = await client.get("/api/sessions/bad.name")
        assert invalid.status_code == 400
        assert invalid.json()["error"].startswith("invalid session name")

        created = await client.post("/api/sessions/work")
        duplicate = await client.post("/api/sessions/work")
        assert created.status_code == 200
        assert duplicate.status_code == 409
        assert duplicate.json() == {"error": "session 'work' already exists"}


@pytest.mark.anyio
async def test_session_store_failures_are_json_api_errors(
    isolated_workspace: tuple[Path, Path],
) -> None:
    _, workspace = isolated_workspace
    app = server.create_app(_options(workspace))
    store = server.SessionStore.for_current_dir("broken")
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not-json", encoding="utf-8")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/sessions/broken")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"].startswith("failed to parse session file")


@pytest.mark.anyio
async def test_malformed_websocket_json_returns_error_and_connection_remains_usable(
    isolated_workspace: tuple[Path, Path],
) -> None:
    _, workspace = isolated_workspace
    app = server.create_app(_options(workspace))

    class FakeWebSocket:
        def __init__(self) -> None:
            self.receives = 0
            self.sent: list[dict[str, object]] = []
            self.headers: dict[str, str] = {}
            self.url = type("Url", (), {"scheme": "ws"})()

        async def accept(self) -> None:
            pass

        async def close(self, code: int = 1000) -> None:
            del code

        async def send_json(self, message: dict[str, object]) -> None:
            self.sent.append(message)

        async def receive_json(self) -> dict[str, object]:
            self.receives += 1
            if self.receives == 1:
                raise ValueError("malformed JSON")
            await asyncio.sleep(0.01)
            if self.receives == 2:
                return {
                    "type": "start_turn",
                    "data": {"request_id": "request-after-error", "prompt": "   "},
                }
            raise WebSocketDisconnect

    route = next(
        route for route in app.routes if getattr(route, "path", None) == "/api/sessions/{name}/ws"
    )
    websocket = FakeWebSocket()
    await route.endpoint(websocket, "default")

    assert [message["type"] for message in websocket.sent] == [
        "snapshot",
        "error",
        "turn_rejected",
    ]
    assert websocket.sent[1] == {
        "type": "error",
        "data": {"message": "invalid websocket message"},
    }
    assert websocket.sent[2] == {
        "type": "turn_rejected",
        "data": {
            "request_id": "request-after-error",
            "reason": "prompt must not be empty",
        },
    }


@pytest.mark.anyio
async def test_rest_mutations_reject_a_running_session(
    isolated_workspace: tuple[Path, Path],
) -> None:
    _, workspace = isolated_workspace
    app = server.create_app(_options(workspace))
    state: server.ServerState = app.state.morrow
    runtime = await state.runtime_for("work")
    worker = asyncio.create_task(_wait_forever())
    runtime.running = server.RunningTurn(
        turn_id="turn-1",
        cancellation=CancellationToken(),
        task=worker,
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            create = await client.post("/api/sessions/work")
            reset = await client.post("/api/sessions/work/reset")
        assert create.status_code == 409
        assert create.json() == {"error": "session has a running turn"}
        assert reset.status_code == 409
        assert reset.json() == {"error": "session has a running turn"}
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.anyio
async def test_client_message_validation_and_concurrent_turn_rejection(
    isolated_workspace: tuple[Path, Path],
) -> None:
    _, workspace = isolated_workspace
    state = server.ServerState(_options(workspace))
    runtime = await state.runtime_for("default")
    queue = runtime.subscribe()
    worker = asyncio.create_task(_wait_forever())
    runtime.running = server.RunningTurn(
        turn_id="turn-existing",
        cancellation=CancellationToken(),
        task=worker,
    )
    try:
        await server._handle_client_message(state, runtime, "default", {"data": {}})
        invalid = await asyncio.wait_for(queue.get(), timeout=1)
        assert invalid == {
            "type": "error",
            "data": {"message": "invalid websocket message"},
        }

        await server._handle_client_message(
            state,
            runtime,
            "default",
            {"type": "start_turn", "data": {"request_id": "request-2", "prompt": "hello"}},
        )
        rejected = await asyncio.wait_for(queue.get(), timeout=1)
        assert rejected == {
            "type": "turn_rejected",
            "data": {
                "request_id": "request-2",
                "reason": "session already has a running turn",
            },
        }
        assert runtime.running is not None
        assert runtime.running.turn_id == "turn-existing"
    finally:
        runtime.unsubscribe(queue)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await state.close()


@pytest.mark.anyio
async def test_wrong_approval_id_preserves_pending_request(
    isolated_workspace: tuple[Path, Path],
) -> None:
    _, workspace = isolated_workspace
    state = server.ServerState(_options(workspace))
    runtime = await state.runtime_for("default")
    queue = runtime.subscribe()
    worker = asyncio.create_task(_wait_forever())
    future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
    runtime.running = server.RunningTurn(
        turn_id="turn-1",
        cancellation=CancellationToken(),
        task=worker,
        pending_approval=server.PendingApproval("approval-call_1", future),
    )
    try:
        await server._resolve_approval(
            state,
            runtime,
            "default",
            "approval-wrong",
            True,
        )
        message = await asyncio.wait_for(queue.get(), timeout=1)
        assert message["type"] == "error"
        assert "does not match pending approval approval-call_1" in message["data"]["message"]
        assert runtime.running.pending_approval is not None
        assert runtime.running.pending_approval.request_id == "approval-call_1"
        assert not future.done()
    finally:
        runtime.unsubscribe(queue)
        future.cancel()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await state.close()

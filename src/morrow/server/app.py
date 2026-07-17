from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from morrow import __version__
from morrow.config import ContextConfig, McpServerConfig, ModelContextLimits
from morrow.core import CancellationToken, Model
from morrow.protocol import (
    ApprovalDecision,
    ApprovalRequest,
    PermissionProfile,
    Session,
    SessionDocument,
)
from morrow.runtime.agent import (
    RunAgentTurnContext,
    RuntimeError,
    TurnEventHandler,
    run_agent_turn_with_cancellation,
)
from morrow.runtime.events import AgentEventEnvelope, timestamp_ms
from morrow.runtime.session_store import (
    SessionEntry,
    SessionNotFound,
    SessionStore,
    SessionStoreError,
)
from morrow.tools.mcp import McpToolCache


@dataclass(slots=True)
class ServerOptions:
    host: str
    port: int
    client: Model
    system_prompt: str
    context_config: ContextConfig
    model_limits: ModelContextLimits
    workspace_root: Path
    config_path: Path
    permissions: PermissionProfile
    mcp_servers: list[McpServerConfig]
    default_session_name: str = "default"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(slots=True)
class PendingApproval:
    request_id: str
    future: asyncio.Future[ApprovalDecision]


@dataclass(slots=True)
class RunningTurn:
    turn_id: str
    cancellation: CancellationToken
    task: asyncio.Task[None]
    pending_approval: PendingApproval | None = None


@dataclass(slots=True)
class SessionRuntime:
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    running: RunningTurn | None = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    def broadcast(self, message: dict[str, Any]) -> None:
        for queue in tuple(self.subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)


class ServerState:
    def __init__(self, options: ServerOptions) -> None:
        self.options = options
        self.sessions: dict[str, SessionRuntime] = {}
        self.lock = asyncio.Lock()
        self.mcp_cache = McpToolCache()

    async def runtime_for(self, session_name: str) -> SessionRuntime:
        async with self.lock:
            return self.sessions.setdefault(session_name, SessionRuntime())

    async def running_snapshot(self, session_name: str) -> dict[str, Any] | None:
        async with self.lock:
            runtime = self.sessions.get(session_name)
            running = runtime.running if runtime else None
            if running is None:
                return None
            return {
                "turn_id": running.turn_id,
                "pending_approval": (
                    running.pending_approval.request_id if running.pending_approval else None
                ),
            }

    async def snapshot(self, session_name: str) -> dict[str, Any]:
        store = _session_store(session_name)
        session = store.load()
        return {
            "type": "snapshot",
            "data": {
                "session": session.to_wire(),
                "running_turn": await self.running_snapshot(session_name),
                "permissions": self.options.permissions.to_wire(),
            },
        }

    async def close(self) -> None:
        close = getattr(self.mcp_cache, "aclose", None) or getattr(self.mcp_cache, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def create_app(options: ServerOptions) -> FastAPI:
    state = ServerState(options)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await state.close()

    app = FastAPI(lifespan=lifespan)
    app.state.morrow = state

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Any, error: ApiError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"error": error.message})

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(_asset_path("index.html").read_text(encoding="utf-8"))

    @app.get("/assets/{asset_path:path}")
    async def asset(asset_path: str) -> Response:
        if asset_path == "app.js":
            return Response(
                _asset_path("app.js").read_bytes(),
                media_type="application/javascript; charset=utf-8",
            )
        if asset_path == "style.css":
            return Response(
                _asset_path("style.css").read_bytes(),
                media_type="text/css; charset=utf-8",
            )
        return Response(status_code=404)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "workspace_root": str(options.workspace_root),
            "config_path": str(options.config_path),
            "permissions": options.permissions.to_wire(),
            "version": __version__,
        }

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        try:
            store = SessionStore.for_current_dir(options.default_session_name)
            entries = store.list_current_scope()
        except SessionStoreError as error:
            raise ApiError(500, str(error)) from error
        return [_session_entry_wire(entry) for entry in entries]

    @app.get("/api/sessions/{name}")
    async def get_session(name: str) -> dict[str, Any]:
        store = _session_store(name)
        try:
            session = store.load()
        except SessionStoreError as error:
            raise ApiError(500, str(error)) from error
        return SessionDocument.new(session).to_wire()

    @app.post("/api/sessions/{name}")
    async def create_session(name: str) -> dict[str, Any]:
        if await state.running_snapshot(name) is not None:
            raise ApiError(409, "session has a running turn")
        store = _session_store(name)
        try:
            store.load_existing()
        except SessionNotFound:
            pass
        except SessionStoreError as error:
            raise ApiError(500, str(error)) from error
        else:
            raise ApiError(409, f"session {name!r} already exists")
        session = Session.new()
        try:
            store.save(session)
        except SessionStoreError as error:
            raise ApiError(500, str(error)) from error
        return SessionDocument.new(session).to_wire()

    @app.post("/api/sessions/{name}/reset")
    async def reset_session(name: str) -> dict[str, Any]:
        if await state.running_snapshot(name) is not None:
            raise ApiError(409, "session has a running turn")
        store = _session_store(name)
        session = Session.new()
        try:
            store.save(session)
        except SessionStoreError as error:
            raise ApiError(500, str(error)) from error
        return SessionDocument.new(session).to_wire()

    @app.websocket("/api/sessions/{name}/ws")
    async def session_ws(websocket: WebSocket, name: str) -> None:
        if not _websocket_origin_allowed(websocket):
            await websocket.close(code=1008, reason="websocket origin not allowed")
            return
        await websocket.accept()
        runtime = await state.runtime_for(name)
        queue = runtime.subscribe()
        sender: asyncio.Task[None] | None = None
        try:
            try:
                await websocket.send_json(await state.snapshot(name))
            except (ApiError, SessionStoreError, OSError, ValueError) as error:
                await websocket.send_json(_error_message(str(error)))
                return
            sender = asyncio.create_task(_socket_sender(websocket, queue))
            while True:
                try:
                    message = await websocket.receive_json()
                except ValueError:
                    runtime.broadcast(_error_message("invalid websocket message"))
                    continue
                await _handle_client_message(state, runtime, name, message)
        except WebSocketDisconnect:
            pass
        except ValueError:
            runtime.broadcast(_error_message("invalid websocket message"))
        finally:
            runtime.unsubscribe(queue)
            if sender is not None:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender

    return app


async def serve(options: ServerOptions) -> None:
    config = uvicorn.Config(
        create_app(options),
        host=options.host,
        port=options.port,
        workers=1,
        log_level="info",
    )
    await uvicorn.Server(config).serve()


async def _socket_sender(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        await websocket.send_json(await queue.get())


async def _handle_client_message(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    message: Any,
) -> None:
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        runtime.broadcast(_error_message("invalid websocket message"))
        return
    data = message.get("data")
    if not isinstance(data, dict):
        runtime.broadcast(_error_message("invalid websocket message"))
        return

    match message["type"]:
        case "start_turn":
            request_id = data.get("request_id")
            prompt = data.get("prompt")
            if not isinstance(request_id, str) or not isinstance(prompt, str):
                runtime.broadcast(_error_message("invalid websocket message"))
                return
            await _start_turn(state, runtime, session_name, request_id, prompt)
        case "approval_decision":
            request_id = data.get("request_id")
            approved = data.get("approved")
            if not isinstance(request_id, str) or not isinstance(approved, bool):
                runtime.broadcast(_error_message("invalid websocket message"))
                return
            await _resolve_approval(state, runtime, session_name, request_id, approved)
        case "cancel_turn":
            turn_id = data.get("turn_id")
            if not isinstance(turn_id, str):
                runtime.broadcast(_error_message("invalid websocket message"))
                return
            await _cancel_turn(state, runtime, session_name, turn_id)
        case _:
            runtime.broadcast(_error_message("invalid websocket message"))


async def _start_turn(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    request_id: str,
    prompt: str,
) -> None:
    if not prompt.strip():
        runtime.broadcast(
            {
                "type": "turn_rejected",
                "data": {"request_id": request_id, "reason": "prompt must not be empty"},
            }
        )
        return
    try:
        _session_store(session_name)
    except ApiError as error:
        runtime.broadcast(
            {
                "type": "turn_rejected",
                "data": {"request_id": request_id, "reason": error.message},
            }
        )
        return

    cancellation = CancellationToken()
    turn_id = f"turn-{timestamp_ms()}"
    async with state.lock:
        if runtime.running is not None:
            runtime.broadcast(
                {
                    "type": "turn_rejected",
                    "data": {
                        "request_id": request_id,
                        "reason": "session already has a running turn",
                    },
                }
            )
            return
        worker = asyncio.create_task(
            _run_turn_task(state, runtime, session_name, turn_id, prompt, cancellation)
        )
        runtime.running = RunningTurn(
            turn_id=turn_id,
            cancellation=cancellation,
            task=worker,
        )
        asyncio.create_task(_supervise_turn(state, runtime, session_name, turn_id, worker))

    runtime.broadcast(await state.snapshot(session_name))


async def _run_turn_task(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    turn_id: str,
    prompt: str,
    cancellation: CancellationToken,
) -> None:
    try:
        store = SessionStore.for_current_dir(session_name)
        session = store.load()
        turn_index = len(session.turns)
        handler = ServerTurnHandler(state, runtime, session_name, turn_id)
        outcome = await run_agent_turn_with_cancellation(
            RunAgentTurnContext(
                client=state.options.client,
                system_prompt=state.options.system_prompt,
                context_config=state.options.context_config,
                model_limits=state.options.model_limits,
                workspace_root=state.options.workspace_root,
                permissions=state.options.permissions,
                mcp_servers=state.options.mcp_servers,
                mcp_cache=state.mcp_cache,
                session_name=session_name,
                turn_index=turn_index,
            ),
            session,
            prompt,
            handler,
            cancellation,
        )
        if outcome.session_changed:
            store.save(session)
            runtime.broadcast(
                {
                    "type": "turn_saved",
                    "data": {"session": session_name, "turn_index": turn_index},
                }
            )
        if outcome.error:
            runtime.broadcast(_error_message(outcome.error))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        runtime.broadcast(_error_message(str(error)))


async def _supervise_turn(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    turn_id: str,
    worker: asyncio.Task[None],
) -> None:
    try:
        await worker
    except asyncio.CancelledError:
        pass
    except BaseException:
        runtime.broadcast(_error_message(f"turn {turn_id} worker panicked"))
    finally:
        await _clear_running(state, runtime, session_name, turn_id)


async def _resolve_approval(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    request_id: str,
    approved: bool,
) -> None:
    del session_name
    async with state.lock:
        running = runtime.running
        if running is None:
            runtime.broadcast(_error_message("session has no running turn"))
            return
        pending = running.pending_approval
        if pending is None:
            runtime.broadcast(_error_message("session has no pending approval"))
            return
        if pending.request_id != request_id:
            runtime.broadcast(
                _error_message(
                    f"approval decision {request_id} does not match pending approval "
                    f"{pending.request_id}"
                )
            )
            return
        running.pending_approval = None

    if not pending.future.done():
        pending.future.set_result(
            ApprovalDecision.approve(request_id) if approved else ApprovalDecision.deny(request_id)
        )


async def _cancel_turn(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    turn_id: str,
) -> None:
    async with state.lock:
        running = runtime.running
        if running is None:
            runtime.broadcast(_error_message("session has no running turn"))
            return
        if running.turn_id != turn_id:
            runtime.broadcast(_error_message(f"turn {turn_id} is not running"))
            return
        running.cancellation.cancel()
    asyncio.create_task(_cancel_fallback(state, runtime, session_name, turn_id))


async def _cancel_fallback(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    turn_id: str,
) -> None:
    await asyncio.sleep(5)
    async with state.lock:
        running = runtime.running
        if running is None or running.turn_id != turn_id or not running.cancellation.is_cancelled:
            return
        task = running.task
        if task.done():
            return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await _clear_running(state, runtime, session_name, turn_id)
    runtime.broadcast(_error_message(f"turn {turn_id} cancellation timed out"))


async def _clear_running(
    state: ServerState,
    runtime: SessionRuntime,
    session_name: str,
    turn_id: str,
) -> None:
    del session_name
    async with state.lock:
        if runtime.running is not None and runtime.running.turn_id == turn_id:
            runtime.running = None


class ServerTurnHandler(TurnEventHandler):
    def __init__(
        self,
        state: ServerState,
        runtime: SessionRuntime,
        session_name: str,
        turn_id: str,
    ) -> None:
        self.state = state
        self.runtime = runtime
        self.session_name = session_name
        self.turn_id = turn_id

    def on_event(self, envelope: AgentEventEnvelope) -> None:
        self.runtime.broadcast({"type": "agent_event", "data": envelope.to_wire()})

    async def resolve_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        async with self.state.lock:
            running = self.runtime.running
            if running is None:
                raise RuntimeError("running turn disappeared")
            if running.turn_id != self.turn_id:
                raise RuntimeError("running turn changed while waiting for approval")
            running.pending_approval = PendingApproval(request.id, future)
        try:
            return await future
        except asyncio.CancelledError:
            return ApprovalDecision.deny(request.id)


def _session_store(name: str) -> SessionStore:
    try:
        return SessionStore.for_current_dir(name)
    except (SessionStoreError, ValueError) as error:
        raise ApiError(400, str(error)) from error


def _session_entry_wire(entry: SessionEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "path": str(entry.path),
        "turns": entry.turns,
        "active_messages": entry.active_messages,
        "summarized_turns": entry.summarized_turns,
        "has_summary": entry.has_summary,
    }


def _error_message(message: str) -> dict[str, Any]:
    return {"type": "error", "data": {"message": message}}


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Allow non-browser clients and same-origin browser WebSocket connections only."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False

    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit(f"//{websocket.headers['host']}")
        expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
        return (
            parsed_origin.scheme == expected_scheme
            and _normalized_authority(parsed_origin, expected_scheme)
            == _normalized_authority(parsed_host, expected_scheme)
            and parsed_origin.path in {"", "/"}
            and not parsed_origin.query
            and not parsed_origin.fragment
        )
    except (KeyError, ValueError):
        return False


def _normalized_authority(parsed: SplitResult, scheme: str) -> tuple[str, int] | None:
    hostname = parsed.hostname
    if hostname is None or parsed.username is not None or parsed.password is not None:
        return None
    default_port = 443 if scheme == "https" else 80
    return hostname.rstrip(".").lower(), parsed.port or default_port


def _asset_path(name: str) -> Path:
    return Path(__file__).with_name("assets") / name


__all__ = ["ServerOptions", "create_app", "serve"]

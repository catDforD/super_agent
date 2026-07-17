"""Official MCP SDK adapter with process-lifetime connection caching."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, TextIO, cast

import httpx

from morrow.core import ToolApproval, ToolExecution, ToolExecutionContext, ToolResult
from morrow.protocol import ToolCall, ToolDefinition, ToolExecutionSummary

from .builtins import TOOL_CANCELLED_ERROR
from .registry import Tool

STDERR_TAIL_BYTES = 8192
MCP_ACTOR_QUEUE_CAPACITY = 64


@dataclass(frozen=True, slots=True)
class _McpServerKey:
    name: str
    transport: str
    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: Path
    url: str | None
    http_headers: tuple[tuple[str, str], ...]
    startup_timeout_sec: int
    tool_timeout_sec: int

    @classmethod
    def from_config(cls, config: Any, cwd: Path) -> _McpServerKey:
        return cls(
            str(config.name),
            _transport_value(config.transport),
            str(config.command),
            tuple(config.args),
            tuple(sorted(dict(config.env).items())),
            cwd,
            config.url,
            tuple(sorted(dict(config.http_headers).items())),
            int(config.startup_timeout_sec),
            int(config.tool_timeout_sec),
        )


@dataclass(frozen=True, slots=True)
class _ListedTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(slots=True)
class _CachedMcpServer:
    runtime: _McpServerRuntime
    listed_tools: list[_ListedTool]


@dataclass(frozen=True, slots=True)
class McpDiscovery:
    tools: list[Tool]
    diagnostics: list[str]


class _McpSessionTerminated(RuntimeError):
    """The remote HTTP session expired and may be retried once after reconnection."""


class McpToolCache:
    """Reuse initialized MCP transports for the lifetime of the Python process."""

    def __init__(self) -> None:
        self._entries: dict[_McpServerKey, asyncio.Task[_CachedMcpServer]] = {}
        self._lock = asyncio.Lock()

    async def get_or_start(self, config: Any, cwd: Path) -> _CachedMcpServer:
        key = _McpServerKey.from_config(config, cwd)
        session_restart_attempted = False
        while True:
            async with self._lock:
                task = self._entries.get(key)
                if task is None or task.get_loop() is not asyncio.get_running_loop():
                    task = asyncio.create_task(
                        _start_mcp_server(config, cwd),
                        name=f"morrow-mcp-start-{config.name}",
                    )
                    self._entries[key] = task
            try:
                entry = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    if self._entries.get(key) is task:
                        self._entries.pop(key, None)
                if isinstance(exc, _McpSessionTerminated) and not session_restart_attempted:
                    session_restart_attempted = True
                    continue
                raise
            if entry.runtime.is_healthy():
                return entry
            async with self._lock:
                if self._entries.get(key) is task:
                    self._entries.pop(key, None)
            await entry.runtime.close()

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._entries.values())
            self._entries.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, _CachedMcpServer):
                await result.runtime.close()

    async def aclose(self) -> None:
        await self.close()


async def discover_tools(
    workspace_root: Path,
    servers: Iterable[Any],
    cache: McpToolCache,
) -> McpDiscovery:
    enabled = [server for server in servers if bool(server.enabled)]

    async def discover(server: Any) -> tuple[str, _CachedMcpServer | Exception]:
        cwd = _resolve_cwd(workspace_root, server.cwd)
        try:
            return str(server.name), await cache.get_or_start(server, cwd)
        except Exception as exc:
            return str(server.name), exc

    results = await asyncio.gather(*(discover(server) for server in enabled))
    tools: list[Tool] = []
    diagnostics: list[str] = []
    emitted_names: set[str] = set()
    for server, (server_name, result) in zip(enabled, results, strict=True):
        if isinstance(result, Exception):
            diagnostics.append(f"mcp server {server_name}: {_safe_error(result)}")
            continue
        definitions, lookup = _build_tool_definitions(
            server_name,
            result.listed_tools,
            emitted_names,
            diagnostics,
        )
        if definitions:
            cwd = _resolve_cwd(workspace_root, server.cwd)

            async def reconnect(server: Any = server, cwd: Path = cwd) -> _CachedMcpServer:
                return await cache.get_or_start(server, cwd)

            tools.append(McpToolProvider(result.runtime, definitions, lookup, reconnect))
    return McpDiscovery(tools, diagnostics)


def build_tool_name(server: str, tool: str) -> str | None:
    server_name = _normalize_component(server)
    tool_name = _normalize_component(tool)
    if not server_name or not tool_name:
        return None
    return f"mcp__{server_name}__{tool_name}"


class McpToolProvider(Tool):
    def __init__(
        self,
        runtime: _McpServerRuntime,
        definitions: list[ToolDefinition],
        lookup: dict[str, str],
        reconnect: Callable[[], Awaitable[_CachedMcpServer]] | None = None,
    ) -> None:
        self.runtime = runtime
        self._definitions = definitions
        self.lookup = lookup
        self._reconnect = reconnect

    def definitions(self) -> list[ToolDefinition]:
        return [definition.model_copy(deep=True) for definition in self._definitions]

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution:
        del approval  # MCP tools are trusted by configuration and never request local approval.
        context = context or ToolExecutionContext()
        cancellation = context.cancellation
        if _is_cancelled(cancellation):
            return ToolExecution.error(TOOL_CANCELLED_ERROR)
        original_name = self.lookup.get(call.function.name)
        if original_name is None:
            return ToolExecution.error(f"unknown MCP tool {call.function.name!r}")
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError as exc:
            return ToolExecution.error(f"invalid arguments for tool {call.function.name}: {exc}")
        if not isinstance(arguments, dict):
            return ToolExecution.error(
                f"invalid arguments for tool {call.function.name}: expected object"
            )

        call_task = asyncio.create_task(
            self._call_tool(original_name, arguments),
            name=f"morrow-mcp-call-{self.runtime.name}-{original_name}",
        )
        cancel_task = asyncio.create_task(_wait_cancelled(cancellation))
        try:
            done, _ = await asyncio.wait(
                {call_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done and call_task not in done:
                call_task.cancel()
                await asyncio.gather(call_task, return_exceptions=True)
                return ToolExecution.error(TOOL_CANCELLED_ERROR)
            cancel_task.cancel()
            try:
                result = await call_task
            except Exception as exc:
                return ToolExecution.completed(_tool_error(_safe_error(exc)))
            return ToolExecution.completed(
                _mcp_call_result(self.runtime.name, original_name, result)
            )
        except asyncio.CancelledError:
            call_task.cancel()
            await asyncio.gather(call_task, return_exceptions=True)
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _call_tool(self, original_name: str, arguments: dict[str, Any]) -> Any:
        runtime = self.runtime
        try:
            return await runtime.call_tool(original_name, arguments)
        except _McpSessionTerminated:
            if self._reconnect is None:
                raise
        await runtime.close()
        entry = await self._reconnect()
        self.runtime = entry.runtime
        return await entry.runtime.call_tool(original_name, arguments)


@dataclass(slots=True)
class _CallCommand:
    tool_name: str
    arguments: dict[str, Any]
    future: asyncio.Future[Any]


class _McpServerRuntime:
    def __init__(self, name: str, tool_timeout: int, startup_timeout: int) -> None:
        self.name = name
        self.tool_timeout = tool_timeout
        self.startup_timeout = startup_timeout
        self.queue: asyncio.Queue[_CallCommand | None] = asyncio.Queue(MCP_ACTOR_QUEUE_CAPACITY)
        self.started: asyncio.Future[list[_ListedTool]] = asyncio.get_running_loop().create_future()
        self.task: asyncio.Task[None] | None = None
        self.healthy = True
        self.read_stream: Any = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self.is_healthy():
            raise RuntimeError("MCP server actor is not healthy")
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(_CallCommand(tool_name, arguments, future))
        return await future

    def is_healthy(self) -> bool:
        if not self.healthy or self.task is None or self.task.done():
            return False
        if self.read_stream is not None:
            try:
                statistics = self.read_stream.statistics()
                if getattr(statistics, "open_send_streams", 1) == 0:
                    return False
            except (AttributeError, RuntimeError):
                pass
        return True

    async def close(self) -> None:
        self.healthy = False
        if self.task is None:
            return
        if self.task.done():
            await asyncio.gather(self.task, return_exceptions=True)
            return
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            self.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 1.0)
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        except asyncio.CancelledError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            pass

    async def run(self, config: Any, cwd: Path) -> None:
        stderr = _StderrCapture()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client
            from mcp.types import Implementation

            transport = _transport_value(config.transport)
            async with AsyncExitStack() as stack:
                if transport == "stdio":
                    params = StdioServerParameters(
                        command=str(config.command),
                        args=list(config.args),
                        env=dict(config.env),
                        cwd=str(cwd),
                    )
                    streams = await stack.enter_async_context(
                        stdio_client(params, errlog=cast(TextIO, stderr.file))
                    )
                elif transport in {"http", "streamable_http", "streamable-http"}:
                    if not config.url:
                        raise ValueError("HTTP MCP server is missing url")
                    http_client = httpx.AsyncClient(
                        headers=dict(config.http_headers),
                        timeout=httpx.Timeout(
                            self.startup_timeout,
                            read=max(self.tool_timeout, self.startup_timeout, 30),
                        ),
                        follow_redirects=True,
                    )
                    await stack.enter_async_context(http_client)
                    streams = await stack.enter_async_context(
                        streamable_http_client(str(config.url), http_client=http_client)
                    )
                else:
                    raise ValueError(f"unsupported MCP transport {transport!r}")

                read_stream, write_stream = streams[0], streams[1]
                self.read_stream = read_stream
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self.tool_timeout),
                    client_info=Implementation(name="morrow", version=_package_version()),
                ) as session:
                    try:
                        await asyncio.wait_for(session.initialize(), self.startup_timeout)
                        listed = await self._list_tools(session)
                    except TimeoutError as exc:
                        raise TimeoutError("MCP startup request timed out") from exc
                    if not self.started.done():
                        self.started.set_result(listed)
                    while True:
                        command = await self.queue.get()
                        if command is None:
                            break
                        if command.future.cancelled():
                            continue
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(
                                    command.tool_name,
                                    command.arguments,
                                    read_timeout_seconds=timedelta(seconds=self.tool_timeout),
                                ),
                                self.tool_timeout,
                            )
                        except TimeoutError:
                            error: Exception = TimeoutError(
                                f"MCP tool {command.tool_name} timed out"
                            )
                            if not command.future.done():
                                command.future.set_exception(error)
                        except Exception as exc:
                            session_terminated = _is_session_terminated(exc)
                            message = stderr.with_tail(_safe_error(exc))
                            if not command.future.done():
                                command_error: Exception = (
                                    _McpSessionTerminated(message)
                                    if session_terminated
                                    else RuntimeError(message)
                                )
                                command.future.set_exception(command_error)
                            if session_terminated or not self._stream_healthy():
                                raise
                        else:
                            if not command.future.done():
                                command.future.set_result(result)
        except asyncio.CancelledError:
            if not self.started.done():
                self.started.cancel()
            raise
        except BaseException as exc:
            message = stderr.with_tail(_safe_error(exc))
            if not self.started.done():
                startup_error: Exception = (
                    _McpSessionTerminated(message)
                    if _is_session_terminated(exc)
                    else RuntimeError(message)
                )
                self.started.set_exception(startup_error)
            self._fail_pending(message)
        finally:
            self.healthy = False
            self._fail_pending("MCP server actor stopped")
            stderr.close()

    async def _list_tools(self, session: Any) -> list[_ListedTool]:
        cursor: str | None = None
        listed: list[_ListedTool] = []
        while True:
            page = await asyncio.wait_for(session.list_tools(cursor=cursor), self.startup_timeout)
            for tool in page.tools:
                listed.append(
                    _ListedTool(
                        name=tool.name,
                        description=tool.description,
                        input_schema=dict(tool.inputSchema or {}),
                    )
                )
            cursor = page.nextCursor or None
            if not cursor:
                return listed

    def _stream_healthy(self) -> bool:
        if self.read_stream is None:
            return False
        try:
            return bool(self.read_stream.statistics().open_send_streams > 0)
        except (AttributeError, RuntimeError):
            return True

    def _fail_pending(self, message: str) -> None:
        while True:
            try:
                command = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if command is not None and not command.future.done():
                command.future.set_exception(RuntimeError(message))


async def _start_mcp_server(config: Any, cwd: Path) -> _CachedMcpServer:
    startup_timeout = int(config.startup_timeout_sec)
    tool_timeout = int(config.tool_timeout_sec)
    if startup_timeout < 1:
        raise ValueError("MCP startup timeout must be greater than zero")
    if tool_timeout < 1:
        raise ValueError("MCP tool timeout must be greater than zero")
    runtime = _McpServerRuntime(str(config.name), tool_timeout, startup_timeout)
    runtime.task = asyncio.create_task(
        runtime.run(config, cwd), name=f"morrow-mcp-actor-{config.name}"
    )
    try:
        # Initialization and every tools/list page have their own startup deadline. An aggregate
        # deadline would incorrectly reject healthy servers with several slow pages.
        listed = await asyncio.shield(runtime.started)
    except BaseException:
        await runtime.close()
        raise
    return _CachedMcpServer(runtime, listed)


def _build_tool_definitions(
    server_name: str,
    tools: list[_ListedTool],
    emitted_names: set[str],
    diagnostics: list[str],
) -> tuple[list[ToolDefinition], dict[str, str]]:
    server_names: set[str] = set()
    definitions: list[ToolDefinition] = []
    lookup: dict[str, str] = {}
    for tool in tools:
        normalized = build_tool_name(server_name, tool.name)
        if normalized is None:
            diagnostics.append(
                f"mcp server {server_name}: skipped tool {tool.name!r}: "
                "normalized tool name is empty"
            )
            continue
        if normalized in server_names:
            diagnostics.append(
                f"mcp server {server_name}: skipped duplicate tool after normalization: "
                f"{normalized}"
            )
            continue
        if normalized in emitted_names:
            diagnostics.append(
                f"mcp server {server_name}: skipped duplicate MCP tool name after "
                f"normalization: {normalized}"
            )
            continue
        server_names.add(normalized)
        emitted_names.add(normalized)
        description = (
            f"MCP tool from server '{server_name}': {tool.description}"
            if tool.description and tool.description.strip()
            else f"MCP tool from server '{server_name}'."
        )
        parameters = tool.input_schema or {"type": "object", "properties": {}}
        definitions.append(ToolDefinition.function(normalized, description, parameters))
        lookup[normalized] = tool.name
    return definitions, lookup


def _mcp_call_result(server: str, tool: str, result: Any) -> ToolResult:
    content = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(item, "model_dump")
        else item
        for item in result.content
    ]
    structured = getattr(result, "structuredContent", None)
    is_error = bool(getattr(result, "isError", False))
    data = {
        "server": server,
        "tool": tool,
        "content": content,
        "structured_content": structured,
        "is_error": is_error,
    }
    if is_error:
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(cast(str, item["text"]))
        error = "\n".join(texts) or "MCP tool returned an error"
        return _tool_error(error, data)
    return ToolResult(
        ok=True,
        content=_json_dumps({"ok": True, "data": data}),
        error=None,
        summary=None,
    )


def _tool_error(error: str, data: dict[str, Any] | None = None) -> ToolResult:
    payload: dict[str, Any] = {"ok": False, "error": error}
    if data is not None:
        payload["data"] = data
    return ToolResult(
        ok=False,
        content=_json_dumps(payload),
        error=error,
        summary=ToolExecutionSummary.error(error),
    )


class _StderrCapture:
    def __init__(self) -> None:
        self.file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - owned until actor exit

    def tail(self) -> str:
        try:
            self.file.flush()
            size = self.file.seek(0, 2)
            self.file.seek(max(0, size - STDERR_TAIL_BYTES))
            return self.file.read().decode("utf-8", errors="replace").strip()
        except (OSError, ValueError):
            return ""

    def with_tail(self, message: str) -> str:
        tail = self.tail()
        return f"{message}; stderr tail: {tail}" if tail else message

    def close(self) -> None:
        self.file.close()


def _resolve_cwd(workspace_root: Path, configured: Any) -> Path:
    if configured is None:
        return workspace_root
    path = Path(configured)
    return path if path.is_absolute() else workspace_root / path


def _transport_value(transport: Any) -> str:
    value = getattr(transport, "value", transport)
    return str(value).lower()


def _normalize_component(value: str) -> str:
    # Match the Rust ASCII-only normalizer instead of allowing Unicode identifiers.
    output = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return output.lower()


def _safe_error(error: BaseException) -> str:
    message = str(error) or error.__class__.__name__
    # SDK/httpx errors occasionally include a full URL. Strip query strings to avoid leaking
    # credentials while retaining the useful endpoint path.
    return re.sub(r"(https?://[^\s?]+)\?[^\s'\"]+", r"\1?<redacted>", message)


def _is_session_terminated(error: BaseException) -> bool:
    if isinstance(error, _McpSessionTerminated):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_is_session_terminated(nested) for nested in error.exceptions)
    detail = getattr(error, "error", None)
    return getattr(detail, "message", None) == "Session terminated"


def _package_version() -> str:
    try:
        return distribution_version("morrow-py")
    except PackageNotFoundError:  # pragma: no cover - source tree without package metadata
        return "0.1.0"


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
    "McpDiscovery",
    "McpToolCache",
    "McpToolProvider",
    "build_tool_name",
    "discover_tools",
]

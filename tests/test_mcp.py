from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import morrow.tools.mcp as mcp_module
from morrow import __version__
from morrow.config import McpServerConfig, McpTransport
from morrow.core import ToolExecutionContext, ToolExecutionType
from morrow.protocol import ToolCall
from morrow.tools import McpToolCache, build_tool_name, discover_tools


def config(name: str, script: Path) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[str(script)],
        env={},
        cwd=None,
        url=None,
        http_headers={},
        enabled=True,
        startup_timeout_sec=2,
        tool_timeout_sec=2,
    )


def http_config(name: str, url: str, headers: dict[str, str] | None = None) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport=McpTransport.HTTP,
        command="",
        args=[],
        env={},
        cwd=None,
        url=url,
        http_headers=headers or {},
        enabled=True,
        startup_timeout_sec=2,
        tool_timeout_sec=2,
    )


class _HttpMcpServer:
    def __init__(self, *, expire_once_on: str | None = None, list_tools_as_sse: bool = False):
        self.expire_once_on = expire_once_on
        self.list_tools_as_sse = list_tools_as_sse
        self.expired = False
        self.session_generation = 0
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                del format, args

            def do_POST(self) -> None:
                owner._handle_post(self)

            def do_GET(self) -> None:
                owner._record(self, None)
                owner._respond(self, 405)

            def do_DELETE(self) -> None:
                owner._record(self, None)
                owner._respond(self, 204)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/mcp"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def post_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [request for request in self.requests if request["method"] == "POST"]

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        size = int(handler.headers.get("content-length", "0"))
        message = json.loads(handler.rfile.read(size) or b"{}")
        self._record(handler, message)
        method = message.get("method")
        if method == self.expire_once_on and not self.expired:
            self.expired = True
            self._respond(handler, 404, b"expired", "text/plain")
            return
        if method == "initialize":
            self.session_generation += 1
            result = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {},
                    "serverInfo": {"name": "fake-http", "version": "1"},
                },
            }
            self._respond_json(
                handler,
                result,
                headers={"Mcp-Session-Id": f"session-{self.session_generation}"},
            )
            return
        if method == "notifications/initialized":
            self._respond(handler, 202)
            return
        if method == "tools/list":
            result = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo text",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
            if self.list_tools_as_sse:
                body = f"event: message\ndata: {json.dumps(result)}\n\n".encode()
                self._respond(handler, 200, body, "text/event-stream")
            else:
                self._respond_json(handler, result)
            return
        if method == "tools/call":
            self._respond_json(
                handler,
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"content": [{"type": "text", "text": "called"}]},
                },
            )
            return
        self._respond(handler, 400)

    def _record(
        self,
        handler: BaseHTTPRequestHandler,
        message: dict[str, Any] | None,
    ) -> None:
        request = {
            "method": handler.command,
            "headers": {name.lower(): value for name, value in handler.headers.items()},
            "message": message,
        }
        with self._lock:
            self.requests.append(request)

    @staticmethod
    def _respond_json(
        handler: BaseHTTPRequestHandler,
        value: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        _HttpMcpServer._respond(
            handler,
            200,
            json.dumps(value).encode(),
            "application/json",
            headers,
        )

    @staticmethod
    def _respond(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        if content_type is not None:
            handler.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if body:
            handler.wfile.write(body)


def test_build_tool_name_normalizes_ascii_components() -> None:
    assert build_tool_name("GitHub Server", "Create Issue!") == ("mcp__github_server__create_issue")
    assert build_tool_name("!!!", "tool") is None


def test_stdio_mcp_cache_pagination_and_tool_call(tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    client_info_marker = tmp_path / "client-info.json"
    script = tmp_path / "server.py"
    script.write_text(
        f"""import json, sys
from pathlib import Path
Path({str(marker)!r}).open('a', encoding='utf-8').write('started\\n')
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        Path({str(client_info_marker)!r}).write_text(
            json.dumps(message['params']['clientInfo']), encoding='utf-8'
        )
        result = {{
            'protocolVersion': message['params']['protocolVersion'],
            'capabilities': {{}},
            'serverInfo': {{'name': 'fake', 'version': '1'}},
        }}
    elif method == 'tools/list':
        cursor = (message.get('params') or {{}}).get('cursor')
        if cursor:
            result = {{'tools': [{{'name': 'fetch', 'inputSchema': {{'type': 'object'}}}}]}}
        else:
            result = {{
                'tools': [{{
                    'name': 'echo',
                    'description': 'Echo text',
                    'inputSchema': {{'type': 'object'}},
                }}],
                'nextCursor': 'next',
            }}
    elif method == 'tools/call':
        result = {{
            'content': [{{'type': 'text', 'text': 'called'}}],
            'structuredContent': {{'value': 1}},
        }}
    else:
        continue
    print(json.dumps({{'jsonrpc': '2.0', 'id': message['id'], 'result': result}}), flush=True)
""",
        encoding="utf-8",
    )

    async def scenario() -> None:
        cache = McpToolCache()
        server = config("Docs", script)
        try:
            first = await discover_tools(tmp_path, [server], cache)
            second = await discover_tools(tmp_path, [server], cache)
            assert first.diagnostics == []
            assert second.diagnostics == []
            assert marker.read_text(encoding="utf-8").splitlines() == ["started"]
            assert json.loads(client_info_marker.read_text(encoding="utf-8")) == {
                "name": "morrow",
                "version": __version__,
            }
            provider = first.tools[0]
            names = [definition.function.name for definition in provider.definitions()]
            assert names == ["mcp__docs__echo", "mcp__docs__fetch"]
            execution = await provider.execute(
                ToolCall.function("call_1", "mcp__docs__echo", '{"text":"hi"}'),
                context=ToolExecutionContext(),
            )
            assert execution.type is ToolExecutionType.COMPLETED
            assert execution.result is not None
            payload = json.loads(execution.result.content)
            assert payload["ok"] is True
            assert payload["data"]["content"][0]["text"] == "called"
            assert payload["data"]["structured_content"] == {"value": 1}
        finally:
            await cache.close()

    asyncio.run(scenario())


def test_http_mcp_streamable_transport_reuses_cached_session(tmp_path: Path) -> None:
    server = _HttpMcpServer(list_tools_as_sse=True)

    async def scenario() -> None:
        cache = McpToolCache()
        remote = http_config(
            "Remote",
            server.url,
            {"Authorization": "Bearer token", "X-Morrow": "static"},
        )
        try:
            first = await discover_tools(tmp_path, [remote], cache)
            second = await discover_tools(tmp_path, [remote], cache)
            assert first.diagnostics == []
            assert second.diagnostics == []
            assert first.tools[0].definitions()[0].function.name == "mcp__remote__echo"

            execution = await first.tools[0].execute(
                ToolCall.function("call_1", "mcp__remote__echo", '{"text":"hi"}')
            )
            assert execution.result is not None
            assert json.loads(execution.result.content)["data"]["content"][0]["text"] == "called"

            posts = server.post_requests()
            methods = [request["message"]["method"] for request in posts]
            assert methods == [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/call",
            ]
            assert "mcp-session-id" not in posts[0]["headers"]
            for request in posts[1:]:
                assert request["headers"]["mcp-session-id"] == "session-1"
            for request in posts:
                assert request["headers"]["authorization"] == "Bearer token"
                assert request["headers"]["x-morrow"] == "static"
        finally:
            await cache.close()

    try:
        asyncio.run(scenario())
    finally:
        server.close()


def test_http_mcp_reinitializes_when_session_expires_during_discovery(
    tmp_path: Path,
) -> None:
    server = _HttpMcpServer(expire_once_on="tools/list")

    async def scenario() -> None:
        cache = McpToolCache()
        try:
            discovery = await discover_tools(tmp_path, [http_config("Remote", server.url)], cache)
            assert discovery.diagnostics == []
            assert len(discovery.tools) == 1
            methods = [request["message"]["method"] for request in server.post_requests()]
            assert methods.count("initialize") == 2
            assert methods.count("tools/list") == 2
        finally:
            await cache.close()

    try:
        asyncio.run(scenario())
    finally:
        server.close()


def test_http_mcp_transparently_retries_after_expired_session(tmp_path: Path) -> None:
    server = _HttpMcpServer(expire_once_on="tools/call")

    async def scenario() -> None:
        cache = McpToolCache()
        remote = http_config("Remote", server.url)
        try:
            first = await discover_tools(tmp_path, [remote], cache)
            recovered = await first.tools[0].execute(
                ToolCall.function("call_1", "mcp__remote__echo", "{}")
            )
            assert recovered.result is not None
            assert recovered.result.ok is True

            second = await discover_tools(tmp_path, [remote], cache)
            assert second.diagnostics == []
            reused = await second.tools[0].execute(
                ToolCall.function("call_2", "mcp__remote__echo", "{}")
            )
            assert reused.result is not None
            assert reused.result.ok is True

            methods = [request["message"]["method"] for request in server.post_requests()]
            assert methods.count("initialize") == 2
            assert methods.count("tools/list") == 2
            assert methods.count("tools/call") == 3
        finally:
            await cache.close()

    try:
        asyncio.run(scenario())
    finally:
        server.close()


def test_runtime_close_waits_for_cancelled_actor_cleanup() -> None:
    async def scenario() -> None:
        runtime = mcp_module._McpServerRuntime("test", tool_timeout=1, startup_timeout=1)
        cleanup_finished = asyncio.Event()

        async def actor() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleanup_finished.set()

        runtime.task = asyncio.create_task(actor())
        await asyncio.sleep(0)
        runtime.queue = asyncio.Queue(maxsize=1)
        runtime.queue.put_nowait(None)

        await runtime.close()

        assert runtime.task.done()
        assert cleanup_finished.is_set()

    asyncio.run(scenario())


def test_runtime_close_propagates_caller_cancellation_after_actor_cleanup() -> None:
    async def scenario() -> None:
        runtime = mcp_module._McpServerRuntime("test", tool_timeout=1, startup_timeout=1)
        cleanup_finished = asyncio.Event()

        async def actor() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_finished.set()

        runtime.task = asyncio.create_task(actor())
        close_task = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        close_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert cleanup_finished.is_set()
        assert runtime.task.done()

    asyncio.run(scenario())


def test_bad_mcp_server_becomes_diagnostic(tmp_path: Path) -> None:
    bad = McpServerConfig(
        name="bad",
        transport=McpTransport.STDIO,
        command="definitely-not-a-real-morrow-command",
        args=[],
        env={},
        cwd=None,
        url=None,
        http_headers={},
        enabled=True,
        startup_timeout_sec=1,
        tool_timeout_sec=1,
    )

    async def scenario() -> None:
        cache = McpToolCache()
        discovery = await discover_tools(tmp_path, [bad], cache)
        assert discovery.tools == []
        assert len(discovery.diagnostics) == 1
        assert discovery.diagnostics[0].startswith("mcp server bad:")
        await cache.close()

    asyncio.run(scenario())

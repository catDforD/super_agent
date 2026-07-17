from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from morrow.core import ModelEvent, ModelEventType
from morrow.model import (
    MAX_HTTP_ERROR_BODY_BYTES,
    ChatCompletionStream,
    EmptyResponse,
    HttpStatusError,
    IncompleteResponse,
    JsonStreamError,
    OpenAiCompatClient,
    OpenAiCompatConfig,
    RequestError,
    StreamEndedBeforeDone,
)
from morrow.protocol import Conversation, Message, ToolDefinition


async def _source(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _collect(stream: AsyncIterator[ModelEvent]) -> list[ModelEvent]:
    return [event async for event in stream]


def test_sse_parser_handles_unicode_byte_splits_and_crlf() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}\r\n\r\n'
        'data: {"choices":[{"delta":{"content":"好"},"finish_reason":null}]}\r\n\r\n'
        "data: [DONE]\r\n\r\n"
    ).encode()
    split_at = body.index("你".encode()) + 1

    events = asyncio.run(
        _collect(ChatCompletionStream(_source([body[:split_at], body[split_at:]])))
    )

    assert events == [
        ModelEvent.text_delta("你"),
        ModelEvent.text_delta("好"),
        ModelEvent.completed(),
    ]


def test_sse_parser_accumulates_fragmented_tool_calls() -> None:
    frames = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'th":"pyproject.toml"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames).encode()

    events = asyncio.run(_collect(ChatCompletionStream(_source([body]))))

    assert len(events) == 1
    assert events[0].type is ModelEventType.TOOL_CALLS
    call = events[0].calls[0]
    assert call.id == "call-1"
    assert call.function.name == "read_file"
    assert call.function.arguments == '{"path":"pyproject.toml"}'


def test_text_is_delivered_before_incomplete_finish_error() -> None:
    body = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n'

    async def run() -> tuple[ModelEvent, Exception]:
        stream = ChatCompletionStream(_source([body]))
        first = await anext(stream)
        try:
            await anext(stream)
        except Exception as error:
            return first, error
        raise AssertionError("incomplete response must raise")

    first, error = asyncio.run(run())

    assert first == ModelEvent.text_delta("partial")
    assert isinstance(error, IncompleteResponse)


def test_malformed_json_and_empty_response_are_explicit_errors() -> None:
    async def run(body: bytes) -> Exception:
        try:
            await _collect(ChatCompletionStream(_source([body])))
        except Exception as error:
            return error
        raise AssertionError("stream must fail")

    malformed = asyncio.run(run(b"data: {not-json}\n\n"))
    empty = asyncio.run(run(b"data: [DONE]\n\n"))

    assert isinstance(malformed, JsonStreamError)
    assert isinstance(empty, EmptyResponse)


def test_stream_end_without_terminal_marker_is_reported_after_text() -> None:
    body = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'

    async def run() -> tuple[ModelEvent, Exception]:
        stream = ChatCompletionStream(_source([body]))
        first = await anext(stream)
        try:
            await anext(stream)
        except Exception as error:
            return first, error
        raise AssertionError("unterminated stream must fail")

    first, error = asyncio.run(run())

    assert first == ModelEvent.text_delta("Hi")
    assert isinstance(error, StreamEndedBeforeDone)


def test_http_adapter_sends_openai_shape_with_tools() -> None:
    captured: dict[str, object] = {}
    body = (
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def run() -> list[ModelEvent]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = OpenAiCompatClient(
            OpenAiCompatConfig("https://example.test/v1", "model", "secret", 5),
            http=http,
        )
        conversation = Conversation(messages=[Message.user("hello")])
        tools = [ToolDefinition.function("read_file", "Read", {"type": "object"})]
        try:
            return await _collect(client.stream_chat(conversation, tools))
        finally:
            await http.aclose()

    events = asyncio.run(run())
    payload = captured["payload"]

    assert events[-1].type is ModelEventType.COMPLETED
    assert isinstance(payload, dict)
    assert payload["model"] == "model"
    assert payload["stream"] is True
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["type"] == "function"
    assert captured["authorization"] == "Bearer secret"


def test_debug_and_request_errors_redact_keys_and_url_queries() -> None:
    config = OpenAiCompatConfig(
        "https://example.test/v1?token=url-secret",
        "model",
        "api-secret",
        5,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    async def run() -> tuple[Exception, str]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = OpenAiCompatClient(config, http=http)
        try:
            await _collect(client.stream_chat(Conversation(messages=[Message.user("hi")])))
        except Exception as error:
            return error, repr(client)
        finally:
            await http.aclose()
        raise AssertionError("request must fail")

    error, client_repr = asyncio.run(run())
    rendered = f"{config!r} {client_repr} {error}"

    assert isinstance(error, RequestError)
    assert "api-secret" not in rendered
    assert "url-secret" not in rendered


def test_http_error_body_is_bounded() -> None:
    oversized = b"x" * (MAX_HTTP_ERROR_BODY_BYTES + 8_192)

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, content=oversized)

    async def run() -> HttpStatusError:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = OpenAiCompatClient(
            OpenAiCompatConfig("https://example.test/v1", "model", "secret", 5),
            http=http,
        )
        try:
            await _collect(client.stream_chat(Conversation(messages=[Message.user("hi")])))
        except HttpStatusError as error:
            return error
        finally:
            await http.aclose()
        raise AssertionError("HTTP error response must fail")

    error = asyncio.run(run())

    assert error.status == 503
    assert error.body.startswith("x" * 100)
    assert error.body.endswith("... error response body truncated ...")
    assert len(error.body.encode("utf-8")) < MAX_HTTP_ERROR_BODY_BYTES + 100

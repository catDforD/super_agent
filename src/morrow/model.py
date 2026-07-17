"""OpenAI-compatible Chat Completions model adapter."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from morrow.core import Model, ModelEvent, ModelRequest
from morrow.protocol import Conversation, Message, ToolCall, ToolDefinition

MAX_HTTP_ERROR_BODY_BYTES = 64 * 1024


class ModelError(RuntimeError):
    """Base error for the OpenAI-compatible adapter."""


class HttpStatusError(ModelError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"model provider returned HTTP {status}: {body}")


class RequestError(ModelError):
    pass


class StreamError(ModelError):
    pass


class Utf8StreamError(ModelError):
    pass


class JsonStreamError(ModelError):
    pass


class StreamEndedBeforeDone(ModelError):
    def __init__(self) -> None:
        super().__init__("model stream ended before data: [DONE]")


class EmptyResponse(ModelError):
    def __init__(self) -> None:
        super().__init__("model returned an empty answer")


class UnsupportedToolCall(ModelError):
    def __init__(self) -> None:
        super().__init__(
            "model requested a legacy function call, but only tool_calls are supported"
        )


class InvalidToolCall(ModelError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"model returned an invalid tool call: {detail}")


class IncompleteResponse(ModelError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"model response was incomplete: finish_reason={reason}")


class UnsupportedFinishReason(ModelError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"model returned an unsupported finish_reason: {reason}")


@dataclass(frozen=True, slots=True)
class OpenAiCompatConfig:
    base_url: str
    model: str
    api_key: str
    timeout: float | timedelta = 120.0

    @property
    def timeout_seconds(self) -> float:
        if isinstance(self.timeout, timedelta):
            return self.timeout.total_seconds()
        return float(self.timeout)

    def __repr__(self) -> str:
        return (
            "OpenAiCompatConfig("
            "base_url='<configured>', "
            f"model={self.model!r}, "
            "api_key='<redacted>', "
            f"timeout={self.timeout!r})"
        )


@dataclass(slots=True)
class _ToolCallAccumulator:
    id: str | None = None
    name: str = ""
    arguments: str = ""


class _SseDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[str]:
        self.buffer.extend(chunk)
        frames: list[str] = []
        while (boundary := _find_sse_frame_end(self.buffer)) is not None:
            frame_end, delimiter_length = boundary
            raw = bytes(self.buffer[:frame_end])
            del self.buffer[: frame_end + delimiter_length]
            try:
                frames.append(raw.decode("utf-8").replace("\r\n", "\n"))
            except UnicodeDecodeError as error:
                raise Utf8StreamError(f"model stream was not valid UTF-8: {error}") from error
        return frames


class ChatCompletionStream(AsyncIterator[ModelEvent]):
    """Incrementally converts response bytes into provider-neutral model events."""

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source
        self._decoder = _SseDecoder()
        self._pending: deque[ModelEvent | ModelError] = deque()
        self._tool_calls: dict[int, _ToolCallAccumulator] = {}
        self._done = False
        self._saw_text = False

    def __aiter__(self) -> ChatCompletionStream:
        return self

    async def aclose(self) -> None:
        self._done = True
        self._pending.clear()
        close = getattr(self._source, "aclose", None)
        if callable(close):
            await close()

    async def __anext__(self) -> ModelEvent:
        if self._pending:
            return self._pop_pending()
        if self._done:
            raise StopAsyncIteration

        while True:
            try:
                chunk = await anext(self._source)
            except StopAsyncIteration as error:
                self._done = True
                raise StreamEndedBeforeDone() from error
            except ModelError:
                self._done = True
                raise
            except Exception as error:
                self._done = True
                raise StreamError(f"failed to read model stream: {error}") from error

            try:
                frames = self._decoder.feed(chunk)
            except ModelError as error:
                self._pending.append(error)
                self._done = True
                return self._pop_pending()
            for frame in frames:
                try:
                    self._handle_frame(frame)
                except ModelError as error:
                    self._pending.append(error)
                    self._done = True
                if self._done:
                    break
            if self._pending:
                return self._pop_pending()
            if self._done:
                raise StopAsyncIteration

    def _pop_pending(self) -> ModelEvent:
        item = self._pending.popleft()
        if isinstance(item, ModelError):
            raise item
        return item

    def _handle_frame(self, frame: str) -> None:
        data = "\n".join(
            value[1:] if value.startswith(" ") else value
            for line in frame.splitlines()
            if (value := line.removeprefix("data:")) != line
        )
        if not data.strip():
            return
        if data.strip() == "[DONE]":
            self._finish_with_completion()
            return

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as error:
            self._done = True
            raise JsonStreamError(f"failed to parse model stream JSON: {error}") from error
        if not isinstance(chunk, dict):
            self._done = True
            raise JsonStreamError("failed to parse model stream JSON: root was not an object")

        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            self._done = True
            raise JsonStreamError("failed to parse model stream JSON: choices was not an array")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                delta = {}
            finish_reason = choice.get("finish_reason")

            if delta.get("function_call") is not None or finish_reason == "function_call":
                self._done = True
                raise UnsupportedToolCall()

            tool_call_deltas = delta.get("tool_calls")
            if tool_call_deltas is not None:
                if not isinstance(tool_call_deltas, list):
                    self._done = True
                    raise InvalidToolCall("tool_calls delta was not an array")
                for tool_call in tool_call_deltas:
                    self._accumulate_tool_call(tool_call)

            content = delta.get("content")
            if isinstance(content, str) and content:
                self._saw_text = True
                self._pending.append(ModelEvent.text_delta(content))

            if finish_reason == "tool_calls":
                self._finish_with_tool_calls()
                return
            if finish_reason == "stop":
                self._finish_with_completion()
                return
            if finish_reason in {"length", "content_filter"}:
                self._done = True
                raise IncompleteResponse(str(finish_reason))
            if finish_reason is not None:
                self._done = True
                raise UnsupportedFinishReason(str(finish_reason))

    def _accumulate_tool_call(self, delta: Any) -> None:
        if not isinstance(delta, dict):
            self._done = True
            raise InvalidToolCall("tool call delta was not an object")
        index = delta.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            self._done = True
            raise InvalidToolCall("tool call delta is missing a valid index")
        kind = delta.get("type")
        if kind is not None and kind != "function":
            self._done = True
            raise InvalidToolCall(f"unsupported tool call type {kind!r}")

        accumulator = self._tool_calls.setdefault(index, _ToolCallAccumulator())
        call_id = delta.get("id")
        if isinstance(call_id, str):
            accumulator.id = call_id
        function = delta.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(name, str):
                accumulator.name += name
            if isinstance(arguments, str):
                accumulator.arguments += arguments

    def _finish_with_tool_calls(self) -> None:
        if not self._tool_calls:
            self._done = True
            raise InvalidToolCall("finish_reason was tool_calls but no tool calls were streamed")

        calls: list[ToolCall] = []
        for index in sorted(self._tool_calls):
            accumulator = self._tool_calls[index]
            if not accumulator.id:
                self._done = True
                raise InvalidToolCall(f"tool call at index {index} is missing id")
            if not accumulator.name:
                self._done = True
                raise InvalidToolCall(f"tool call {accumulator.id} is missing function name")
            calls.append(
                ToolCall.function(
                    accumulator.id,
                    accumulator.name,
                    accumulator.arguments,
                )
            )
        self._tool_calls.clear()
        self._pending.append(ModelEvent.tool_calls(calls))
        self._done = True

    def _finish_with_completion(self) -> None:
        self._done = True
        if not self._saw_text:
            raise EmptyResponse()
        self._pending.append(ModelEvent.completed())


class OpenAiCompatClient(Model):
    """HTTPX-backed adapter for OpenAI-compatible ``/chat/completions`` APIs."""

    def __init__(
        self,
        config: OpenAiCompatConfig,
        *,
        http: httpx.AsyncClient | None = None,
        trust_env: bool = True,
    ) -> None:
        self.config = config
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=config.timeout_seconds,
            trust_env=trust_env,
        )

    @classmethod
    def new(cls, config: OpenAiCompatConfig) -> OpenAiCompatClient:
        return cls(config)

    @classmethod
    def new_without_proxy(cls, config: OpenAiCompatConfig) -> OpenAiCompatClient:
        return cls(config, trust_env=False)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self.stream_chat(request.conversation, request.tools)

    async def stream_chat(
        self,
        conversation: Conversation,
        tools: Iterable[ToolDefinition] = (),
    ) -> AsyncIterator[ModelEvent]:
        tool_list = list(tools)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [_message_wire(message) for message in conversation.messages],
            "stream": True,
        }
        if tool_list:
            payload["tools"] = [_model_wire(tool) for tool in tool_list]
            payload["tool_choice"] = "auto"

        try:
            async with self._http.stream(
                "POST",
                self.chat_completions_url,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload,
            ) as response:
                if not response.is_success:
                    body = await _read_http_error_body(response, self.config)
                    raise HttpStatusError(response.status_code, body)

                async def response_bytes() -> AsyncIterator[bytes]:
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    except httpx.HTTPError as error:
                        message = _redact_secrets(str(error), self.config)
                        raise StreamError(f"failed to read model stream: {message}") from error

                async for event in ChatCompletionStream(response_bytes()):
                    yield event
        except ModelError:
            raise
        except httpx.HTTPError as error:
            message = _redact_secrets(str(error), self.config)
            raise RequestError(f"failed to send model request: {message}") from error

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> OpenAiCompatClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"OpenAiCompatClient(config={self.config!r})"


def _find_sse_frame_end(buffer: bytes | bytearray) -> tuple[int, int] | None:
    for index in range(len(buffer)):
        if buffer[index : index + 4] == b"\r\n\r\n":
            return index, 4
        if buffer[index : index + 2] == b"\n\n":
            return index, 2
    return None


def _model_wire(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


def _message_wire(message: Message) -> dict[str, Any]:
    value = _model_wire(message)
    # OpenAI requires the content key on assistant tool-call messages, where it is null.
    value.setdefault("content", None)
    return value


async def _read_http_error_body(
    response: httpx.Response,
    config: OpenAiCompatConfig,
) -> str:
    body = bytearray()
    truncated = False
    try:
        async for chunk in response.aiter_bytes():
            remaining = MAX_HTTP_ERROR_BODY_BYTES - len(body)
            if len(chunk) > remaining:
                body.extend(chunk[:remaining])
                truncated = True
                break
            body.extend(chunk)
    except httpx.HTTPError as error:
        message = _redact_secrets(str(error), config)
        return f"failed to read error body: {message}"

    rendered = body.decode("utf-8", errors="replace")
    if truncated:
        rendered += "\n... error response body truncated ..."
    return _redact_secrets(rendered, config)


_QUERY_SECRET = re.compile(r"([?&][^=\s&]+)=([^\s&#]+)")


def _redact_secrets(message: str, config: OpenAiCompatConfig) -> str:
    redacted = message.replace(config.api_key, "<redacted>") if config.api_key else message
    redacted = _QUERY_SECRET.sub(r"\1=<redacted>", redacted)
    return redacted.replace(config.base_url, "<configured-url>")


__all__ = [
    "ChatCompletionStream",
    "EmptyResponse",
    "HttpStatusError",
    "IncompleteResponse",
    "InvalidToolCall",
    "JsonStreamError",
    "MAX_HTTP_ERROR_BODY_BYTES",
    "ModelError",
    "OpenAiCompatClient",
    "OpenAiCompatConfig",
    "RequestError",
    "StreamEndedBeforeDone",
    "StreamError",
    "UnsupportedFinishReason",
    "UnsupportedToolCall",
    "Utf8StreamError",
]

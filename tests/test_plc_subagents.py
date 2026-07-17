from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from morrow.config import ContextConfig, ModelContextLimits, PlcSubagentConfig
from morrow.core import (
    EMPTY_TOOL_RUNTIME,
    CancellationToken,
    ModelEvent,
    ModelRequest,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
    ToolExecutionType,
)
from morrow.protocol import (
    AgentEventType,
    PermissionProfile,
    Session,
    ToolCall,
)
from morrow.runtime.agent import (
    RunAgentTurnContext,
    TurnEventHandler,
    _build_tools,
    run_agent_turn,
)
from morrow.tools.mcp import McpToolCache
from morrow.tools.plc_subagents import (
    MAX_DIAGNOSTIC_CHARS,
    MAX_DIAGNOSTICS,
    MAX_HTTP_ERROR_BYTES,
    MAX_SSE_FRAME_BYTES,
    PLC_SUBAGENT_GUIDANCE,
    PlcSubagentTools,
)
from morrow.tools.registry import ToolRegistry


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class ScriptedModel:
    def __init__(self, scripts: list[list[ModelEvent]]) -> None:
        self.scripts = deque(scripts)
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        script = self.scripts.popleft()

        async def generate() -> AsyncIterator[ModelEvent]:
            for event in script:
                yield event

        return generate()


class RecordingHandler(TurnEventHandler):
    def __init__(self) -> None:
        self.types: list[AgentEventType] = []

    def on_event(self, event) -> None:  # type: ignore[no-untyped-def]
        self.types.append(event.event.type)


def tool_call(name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall.function("call-1", name, json.dumps(arguments, ensure_ascii=False))


def event_frame(value: dict[str, Any], newline: bytes = b"\n") -> bytes:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    return b"data: " + payload + newline + newline


def sse_response(
    events: list[dict[str, Any]],
    *,
    chunks: list[bytes] | None = None,
    status_code: int = 200,
    content_type: str = "text/event-stream",
) -> httpx.Response:
    body = b"".join(event_frame(event) for event in events)
    return httpx.Response(
        status_code,
        headers={"Content-Type": content_type},
        stream=ChunkStream(chunks if chunks is not None else [body]),
    )


def provider_for(
    handler: Callable[[httpx.Request], Any],
    *,
    timeout_secs: int = 30,
) -> PlcSubagentTools:
    identifiers = iter(str(index) for index in range(1, 100))
    return PlcSubagentTools(
        PlcSubagentConfig(
            enabled=True,
            base_url="https://plc.example/root",
            timeout_secs=timeout_secs,
        ),
        transport=httpx.MockTransport(handler),
        id_factory=lambda: next(identifiers),
    )


def result_json(execution: ToolExecution) -> dict[str, Any]:
    assert execution.type is ToolExecutionType.COMPLETED
    assert execution.result is not None
    return json.loads(execution.result.content)


def develop_events(*, compile_ok: bool = True) -> list[dict[str, Any]]:
    return [
        {"type": "session_id", "session_id": "remote-session"},
        {
            "type": "st_code_json",
            "stCode": {
                "code": "FUNCTION_BLOCK Demo\nEND_FUNCTION_BLOCK",
                "file_name": "demo.st",
                "language": "ST",
                "compile_ok": compile_ok,
            },
        },
    ]


def repair_events(*, success: bool = True) -> list[dict[str, Any]]:
    return [
        {"type": "session_id", "session_id": "remote-repair"},
        {
            "type": "compilation_report_json",
            "content": {
                "compilation_success": success,
                "code_file": "fixed.st",
                "report_id": "compile-1",
                "errors": [] if success else [{"message": "missing semicolon"}],
                "attempt_count": 1,
                "fixes_applied": ["added semicolon"],
            },
        },
    ]


def formal_events(statuses: list[str]) -> list[dict[str, Any]]:
    return [
        {"type": "session_id", "session_id": "remote-formal"},
        {
            "type": "formal_report_json",
            "content": {
                "report_id": "formal-1",
                "property_count": len(statuses),
                "properties": [
                    {"status": status, "summary": f"property {index}"}
                    for index, status in enumerate(statuses, start=1)
                ],
                "artifacts": {"download_json_url": "/reports/formal-1.json"},
            },
        },
    ]


def fuzz_events(
    *,
    total: int = 1,
    passed: int = 1,
    failed: int = 0,
    workflow_success: bool = True,
    failed_details: list[Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        {"type": "session_id", "session_id": "remote-test"},
        {
            "type": "fuzz_report_json",
            "content": {
                "report_id": "fuzz-1",
                "workflow_success": workflow_success,
                "execution_backend": "real",
                "compile_backend": "matiec",
                "fuzz_method": "legacy",
                "summary": {
                    "total_test_cases": total,
                    "success_cases": passed,
                    "failed_cases": failed,
                },
                "coverage_statistics": {"branch_pct": 75.0},
                "failed_details": failed_details or [],
                "artifacts": {"download_html_url": "/reports/fuzz-1.html"},
            },
        },
    ]


def test_definitions_are_strict_and_all_plc_calls_are_serial() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("no request expected")

    provider = provider_for(handler)
    definitions = provider.definitions()

    assert [definition.function.name for definition in definitions] == [
        "plc_develop",
        "plc_repair",
        "plc_formal_verify",
        "plc_test",
    ]
    for definition in definitions:
        assert definition.function.parameters["additionalProperties"] is False
        assert provider.execution_mode(tool_call(definition.function.name, {})) is (
            ToolExecutionMode.SERIAL
        )
    develop_schema = definitions[0].function.parameters
    assert develop_schema["properties"]["target_language"]["enum"] == ["ST", "SCL"]
    formal_variants = definitions[2].function.parameters["properties"]["properties"]["items"][
        "oneOf"
    ]
    assert len(formal_variants) == 4
    assertion = formal_variants[0]
    assert "pattern_id" not in assertion["properties"]
    implication = next(
        variant
        for variant in formal_variants
        if variant["properties"].get("pattern_id", {}).get("const") == "pattern-implication"
    )
    assert implication["properties"]["pattern_params"]["required"] == ["1", "2"]
    test_schema = definitions[3].function.parameters
    assert test_schema["properties"]["case_count"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 10,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("plc_develop", {"requirement": ""}),
        ("plc_develop", {"requirement": "code", "target_language": "FBD"}),
        ("plc_develop", {"requirement": "code", "target_language": "st"}),
        ("plc_repair", {"st_code": ""}),
        ("plc_repair", {"st_code": "x", "failure_notes": None}),
        ("plc_repair", {"st_code": "x", "unknown": True}),
        ("plc_test", {"st_code": "x", "case_count": 0}),
        ("plc_test", {"st_code": "x", "case_count": 101}),
        ("plc_test", {"st_code": "x", "case_count": "2"}),
        ("plc_test", {"st_code": "x", "case_count": True}),
        ("plc_formal_verify", {"st_code": "x", "properties": []}),
        (
            "plc_formal_verify",
            {
                "st_code": "x",
                "properties": [
                    {
                        "description": "assert",
                        "job_req": "assertion",
                        "entry_point": "Demo",
                    }
                ],
            },
        ),
        (
            "plc_formal_verify",
            {
                "st_code": "//#ASSERT x : label",
                "properties": [
                    {
                        "description": "assert",
                        "job_req": "assertion",
                        "entry_point": "Demo",
                        "pattern_id": None,
                    }
                ],
            },
        ),
        (
            "plc_formal_verify",
            {
                "st_code": "x",
                "properties": [
                    {
                        "description": "implication",
                        "job_req": "pattern",
                        "entry_point": "Demo",
                        "pattern_id": "pattern-implication",
                        "pattern_params": {"1": "x"},
                    }
                ],
            },
        ),
        (
            "plc_formal_verify",
            {
                "st_code": "x",
                "properties": [
                    {
                        "description": "invariant",
                        "job_req": "pattern",
                        "entry_point": "Demo",
                        "pattern_id": "pattern-invariant",
                        "pattern_params": {"1": "x", "2": "y"},
                    }
                ],
            },
        ),
    ],
)
async def test_invalid_tool_arguments_are_rejected_before_network(
    name: str, arguments: dict[str, Any]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("invalid arguments must not reach the network")

    execution = await provider_for(handler).execute(tool_call(name, arguments))
    output = result_json(execution)

    assert execution.result is not None
    assert execution.result.ok is False
    assert output["ok"] is False
    assert "invalid arguments" in output["error"]


@pytest.mark.anyio
async def test_formal_property_limit_is_twenty() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("invalid arguments must not reach the network")

    properties = [
        {
            "description": f"property {index}",
            "job_req": "pattern",
            "entry_point": "Demo",
            "pattern_id": "pattern-invariant",
            "pattern_params": {"1": "x"},
        }
        for index in range(21)
    ]
    execution = await provider_for(handler).execute(
        tool_call("plc_formal_verify", {"st_code": "x", "properties": properties})
    )

    assert result_json(execution)["ok"] is False


@pytest.mark.anyio
async def test_exact_remote_payloads_and_fixed_contexts() -> None:
    requests: list[dict[str, Any]] = []
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["content-type"].startswith("application/json")
        payload = json.loads((await request.aread()).decode())
        requests.append(payload)
        agent_id = payload["agent_id"]
        if agent_id == "retrieval_planning_coding_agent":
            events = develop_events()
            events[1]["stCode"]["language"] = "SCL"
        elif agent_id == "compilation_debugging_agent":
            events = repair_events()
        elif agent_id == "formal_validation_agent":
            property_count = len(json.loads(payload["message"])["properties"])
            events = formal_events(["PASS"] * property_count)
        else:
            events = fuzz_events(total=3, passed=3)
        events[0]["session_id"] = payload["session_id"]
        return sse_response(events)

    provider = provider_for(handler)
    await provider.execute(
        tool_call(
            "plc_develop",
            {"requirement": "build a timer", "target_language": "SCL"},
        )
    )
    await provider.execute(
        tool_call(
            "plc_repair",
            {"st_code": "PROGRAM P END_PROGRAM", "failure_notes": "missing semicolon"},
        )
    )
    formal_properties = [
        {
            "description": "inline assertion",
            "job_req": "assertion",
            "entry_point": "Demo",
        },
        {
            "description": "implication",
            "job_req": "pattern",
            "entry_point": "Demo",
            "pattern_id": "pattern-implication",
            "pattern_params": {"1": "instance.x", "2": "instance.y"},
        },
        {
            "description": "forbidden",
            "job_req": "pattern",
            "entry_point": "Demo",
            "pattern_id": "pattern-forbidden",
            "pattern_params": {"1": "instance.error"},
        },
        {
            "description": "invariant",
            "job_req": "pattern",
            "entry_point": "Demo",
            "pattern_id": "pattern-invariant",
            "pattern_params": {"1": "instance.ready"},
        },
    ]
    await provider.execute(
        tool_call(
            "plc_formal_verify",
            {
                "st_code": "FUNCTION_BLOCK Demo\n//#ASSERT x : inline\nEND_FUNCTION_BLOCK",
                "properties": formal_properties,
            },
        )
    )
    await provider.execute(
        tool_call("plc_test", {"st_code": "PROGRAM P END_PROGRAM", "case_count": 3})
    )

    assert paths == ["/root/api/chat/stream"] * 4
    assert all(request["uploaded_files"] == [] for request in requests)
    assert all(request["language"] == "zh-CN" for request in requests)
    assert all("agentId" not in request and "sessionId" not in request for request in requests)
    assert len({request["session_id"] for request in requests}) == 4
    assert len({request["user_id"] for request in requests}) == 4
    assert requests[0]["message"] == "build a timer"
    assert requests[0]["context"] == {
        "target_language": "SCL",
        "compiler_type": "matiec",
        "enable_socratic_spec": False,
    }
    assert json.loads(requests[1]["message"]) == {"st_code": "PROGRAM P END_PROGRAM"}
    assert requests[1]["context"] == {
        "repair_source": "compile",
        "compiler_type": "matiec",
        "repair_failure_notes": "missing semicolon",
    }
    remote_formal = json.loads(requests[2]["message"])
    assert remote_formal["properties"] == [
        {
            "property_description": "inline assertion",
            "property": {"job_req": "assertion", "entry_point": "Demo"},
        },
        {
            "property_description": "implication",
            "property": {
                "job_req": "pattern",
                "entry_point": "Demo",
                "pattern_id": "pattern-implication",
                "pattern_params": {"1": "instance.x", "2": "instance.y"},
            },
        },
        {
            "property_description": "forbidden",
            "property": {
                "job_req": "pattern",
                "entry_point": "Demo",
                "pattern_id": "pattern-forbidden",
                "pattern_params": {"1": "instance.error"},
            },
        },
        {
            "property_description": "invariant",
            "property": {
                "job_req": "pattern",
                "entry_point": "Demo",
                "pattern_id": "pattern-invariant",
                "pattern_params": {"1": "instance.ready"},
            },
        },
    ]
    assert json.loads(requests[3]["message"]) == {"st_code": "PROGRAM P END_PROGRAM"}
    assert requests[3]["context"] == {"fuzz_method": "legacy", "case_count": 3}


@pytest.mark.anyio
async def test_sse_supports_chunk_splits_crlf_multiline_data_and_eof() -> None:
    session = b'data: {"type":"session_id",\r\ndata: "session_id":"remote-split"}\r\n\r\n'
    report = event_frame(
        {
            "type": "st_code_json",
            "stCode": {
                "code": "FUNCTION_BLOCK Split\nEND_FUNCTION_BLOCK",
                "file_name": "split.st",
                "language": "ST",
                "compile_ok": True,
            },
        },
        newline=b"\r\n",
    )
    body = session + report
    chunks = [body[index : index + 3] for index in range(0, len(body), 3)]

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(
            [],
            chunks=chunks,
            content_type="text/event-stream; charset=utf-8",
        )

    output = result_json(
        await provider_for(handler).execute(
            tool_call("plc_develop", {"requirement": "split stream"})
        )
    )

    assert output["ok"] is True
    assert output["data"]["remote_session_id"] == "remote-split"
    assert output["data"]["status"] == "generated_compiled"


@pytest.mark.anyio
async def test_diagnostics_are_bounded_and_last_stage_guidance_is_retained() -> None:
    diagnostics = [
        {"type": "token", "content": f"❌ issue {index} " + ("x" * 3_000)} for index in range(25)
    ]
    events = [
        {"type": "session_id", "session_id": "remote-diagnostics"},
        *diagnostics,
        {"type": "stage_guidance", "content": "first"},
        {"type": "stage_guidance", "content": "last"},
        develop_events()[1],
    ]
    del events[-1]["stCode"]["compile_ok"]

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(events)

    output = result_json(
        await provider_for(handler).execute(
            tool_call("plc_develop", {"requirement": "diagnostics"})
        )
    )

    assert output["ok"] is True
    assert output["data"]["status"] == "generated_uncompiled"
    assert len(output["data"]["diagnostics"]) == MAX_DIAGNOSTICS
    assert all(
        len(diagnostic) <= MAX_DIAGNOSTIC_CHARS for diagnostic in output["data"]["diagnostics"]
    )
    assert output["data"]["result"]["stage_guidance"] == "last"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"data: {not-json}\n\n", "malformed JSON"),
        (
            b"".join(event_frame(event) for event in develop_events() + develop_events()[1:]),
            "multiple st_code_json",
        ),
        (event_frame(develop_events()[1]), "did not provide a session_id"),
        (
            event_frame({"type": "session_id", "session_id": "remote"})
            + event_frame({"type": "token", "content": "❌ execution failed"}),
            "did not provide st_code_json",
        ),
    ],
)
async def test_protocol_errors_are_tool_errors(body: bytes, expected: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response([], chunks=[body])

    execution = await provider_for(handler).execute(
        tool_call("plc_develop", {"requirement": "protocol"})
    )
    output = result_json(execution)

    assert execution.result is not None
    assert execution.result.ok is False
    assert expected in output["error"]


@pytest.mark.anyio
async def test_oversized_sse_frame_is_rejected() -> None:
    body = b"data: " + (b"x" * (MAX_SSE_FRAME_BYTES + 1))

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response([], chunks=[body])

    output = result_json(
        await provider_for(handler).execute(tool_call("plc_develop", {"requirement": "oversized"}))
    )

    assert output["ok"] is False
    assert "SSE frame exceeds" in output["error"]


@pytest.mark.anyio
async def test_http_errors_and_wrong_content_type_are_bounded_protocol_errors() -> None:
    error_body = b"remote failure " + (b"x" * (MAX_HTTP_ERROR_BYTES + 1_000))

    async def http_error(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, stream=ChunkStream([error_body]))

    output = result_json(
        await provider_for(http_error).execute(
            tool_call("plc_develop", {"requirement": "http error"})
        )
    )
    assert output["ok"] is False
    assert "HTTP 503" in output["error"]
    assert len(output["error"].encode()) < MAX_HTTP_ERROR_BYTES + 200
    assert output["error"].endswith("…")

    async def wrong_type(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )

    output = result_json(
        await provider_for(wrong_type).execute(
            tool_call("plc_develop", {"requirement": "wrong type"})
        )
    )
    assert output["ok"] is False
    assert "Content-Type" in output["error"]


@pytest.mark.anyio
async def test_total_timeout_and_cancellation_stop_the_remote_request() -> None:
    async def wait_forever(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    timeout_provider = provider_for(wait_forever, timeout_secs=0.01)  # type: ignore[arg-type]
    timed_out = result_json(
        await timeout_provider.execute(tool_call("plc_develop", {"requirement": "timeout"}))
    )
    assert timed_out["ok"] is False
    assert "timed out" in timed_out["error"]

    started = asyncio.Event()

    async def cancellable(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    cancellation = CancellationToken()
    task = asyncio.create_task(
        provider_for(cancellable).execute(
            tool_call("plc_develop", {"requirement": "cancel"}),
            context=ToolExecutionContext(cancellation),
        )
    )
    await started.wait()
    cancellation.cancel()
    cancelled = result_json(await asyncio.wait_for(task, timeout=1))
    assert cancelled["ok"] is False
    assert cancelled["error"] == "tool execution cancelled"


@pytest.mark.anyio
async def test_repair_business_failure_is_successful_diagnostic_result() -> None:
    events = repair_events(success=False)
    events[1]["content"]["artifacts"] = {"report_url": "/reports/compile-1"}

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(events)

    execution = await provider_for(handler).execute(
        tool_call("plc_repair", {"st_code": "PROGRAM Broken END_PROGRAM"})
    )
    output = result_json(execution)

    assert execution.result is not None
    assert execution.result.ok is True
    assert output["data"]["agent"] == "repair"
    assert output["data"]["status"] == "not_repaired"
    assert output["data"]["result"]["compilation_success"] is False
    assert output["data"]["result"]["fixed_code"] is None
    assert "does not expose" in output["data"]["result"]["fixed_code_note"]
    assert output["data"]["artifacts"] == [
        {
            "name": "report_url",
            "url": "https://plc.example/reports/compile-1",
        }
    ]


@pytest.mark.anyio
async def test_formal_mixed_results_are_normalized_and_count_drift_is_rejected() -> None:
    properties = [
        {
            "description": f"property {index}",
            "job_req": "pattern",
            "entry_point": "Demo",
            "pattern_id": "pattern-invariant",
            "pattern_params": {"1": f"x{index}"},
        }
        for index in range(3)
    ]
    events = formal_events(["PASS", "FAIL", "NOT_CHECKED"])
    for item in events[1]["content"]["properties"]:
        item["summary"] = json.dumps({"backend_used": "nusmv", "detail": item["summary"]})
    events[1]["content"]["properties"][1]["counterexample"] = {"summary": "x became false"}

    async def mixed_handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(events)

    output = result_json(
        await provider_for(mixed_handler).execute(
            tool_call("plc_formal_verify", {"st_code": "x", "properties": properties})
        )
    )
    assert output["ok"] is True
    assert output["data"]["status"] == "mixed"
    assert output["data"]["result"]["passed"] == 1
    assert output["data"]["result"]["failed"] == 1
    assert output["data"]["result"]["not_checked"] == 1
    assert output["data"]["result"]["backend"] == "nusmv"
    assert output["data"]["result"]["properties"][0]["backend"] == "nusmv"
    assert output["data"]["result"]["properties"][1]["counterexample_summary"] == ("x became false")
    assert output["data"]["artifacts"][0]["url"] == ("https://plc.example/reports/formal-1.json")

    async def drift_handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(formal_events(["PASS"]))

    drift = result_json(
        await provider_for(drift_handler).execute(
            tool_call(
                "plc_formal_verify",
                {"st_code": "x", "properties": properties[:2]},
            )
        )
    )
    assert drift["ok"] is False
    assert "property count does not match" in drift["error"]


@pytest.mark.anyio
async def test_test_agent_failure_partial_and_output_trimming() -> None:
    failed_details = [{"case": index, "message": "failure " + ("x" * 3_000)} for index in range(25)]
    events = fuzz_events(total=2, passed=1, failed=1, failed_details=failed_details)
    events[1]["content"]["generated_testcases"] = ["secret-large-field"]
    events[1]["content"]["markdown"] = "secret-markdown"

    async def failure_handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(events)

    output = result_json(
        await provider_for(failure_handler).execute(
            tool_call("plc_test", {"st_code": "PROGRAM P END_PROGRAM", "case_count": 2})
        )
    )
    encoded = json.dumps(output)
    assert output["ok"] is True
    assert output["data"]["status"] == "failed"
    assert len(output["data"]["result"]["failed_details"]) == 20
    assert output["data"]["result"]["failed_details_truncated"] is True
    assert "secret-large-field" not in encoded
    assert "secret-markdown" not in encoded
    assert output["data"]["artifacts"][0]["url"] == ("https://plc.example/reports/fuzz-1.html")

    async def partial_handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(fuzz_events(total=1, passed=1, workflow_success=False))

    partial = result_json(
        await provider_for(partial_handler).execute(
            tool_call("plc_test", {"st_code": "PROGRAM P END_PROGRAM", "case_count": 2})
        )
    )
    assert partial["ok"] is True
    assert partial["data"]["status"] == "partial"


@pytest.mark.anyio
async def test_runtime_enablement_changes_tools_and_prompt_only_when_enabled(
    tmp_path: Path,
) -> None:
    async def build_names(config: PlcSubagentConfig) -> list[str]:
        cache = McpToolCache()
        try:
            build = await _build_tools(
                RunAgentTurnContext(
                    client=ScriptedModel([[ModelEvent.completed()]]),
                    system_prompt="system",
                    context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
                    model_limits=ModelContextLimits(16_000, 1_000),
                    workspace_root=tmp_path,
                    permissions=PermissionProfile(),
                    plc_subagents=config,
                    mcp_cache=cache,
                ),
                CancellationToken(),
            )
            return [definition.function.name for definition in build.registry.definitions()]
        finally:
            await cache.aclose()

    disabled_names = await build_names(PlcSubagentConfig())
    enabled_config = PlcSubagentConfig(
        enabled=True,
        base_url="https://plc.example",
        timeout_secs=30,
    )
    enabled_names = await build_names(enabled_config)
    assert enabled_names == disabled_names + [
        "plc_develop",
        "plc_repair",
        "plc_formal_verify",
        "plc_test",
    ]

    disabled_model = ScriptedModel([[ModelEvent.completed()]])
    disabled_session = Session.new()
    await run_agent_turn(
        RunAgentTurnContext(
            client=disabled_model,
            system_prompt="user system prompt",
            context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
            model_limits=ModelContextLimits(16_000, 1_000),
            workspace_root=tmp_path,
            permissions=PermissionProfile(),
            tool_runtime=EMPTY_TOOL_RUNTIME,
        ),
        disabled_session,
        "hello",
        RecordingHandler(),
    )
    disabled_prompt = disabled_model.requests[0].conversation.messages[0].content
    assert disabled_prompt == "user system prompt"

    enabled_model = ScriptedModel([[ModelEvent.completed()]])
    enabled_session = Session.new()
    await run_agent_turn(
        RunAgentTurnContext(
            client=enabled_model,
            system_prompt="user system prompt",
            context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
            model_limits=ModelContextLimits(16_000, 1_000),
            workspace_root=tmp_path,
            permissions=PermissionProfile(),
            plc_subagents=enabled_config,
            tool_runtime=EMPTY_TOOL_RUNTIME,
        ),
        enabled_session,
        "hello",
        RecordingHandler(),
    )
    enabled_prompt = enabled_model.requests[0].conversation.messages[0].content
    assert enabled_prompt is not None
    assert enabled_prompt.startswith("user system prompt\n\n")
    assert enabled_prompt.count(PLC_SUBAGENT_GUIDANCE) == 1


@pytest.mark.anyio
async def test_scripted_main_agent_receives_normalized_plc_tool_result(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return sse_response(develop_events())

    registry = ToolRegistry.empty()
    registry.register(provider_for(handler))
    model = ScriptedModel(
        [
            [
                ModelEvent.tool_calls(
                    [
                        ToolCall.function(
                            "plc-call",
                            "plc_develop",
                            json.dumps({"requirement": "build a block"}),
                        )
                    ]
                )
            ],
            [ModelEvent.text_delta("engineering complete"), ModelEvent.completed()],
        ]
    )
    handler_events = RecordingHandler()
    session = Session.new()

    outcome = await run_agent_turn(
        RunAgentTurnContext(
            client=model,
            system_prompt="system",
            context_config=ContextConfig(False, 0.8, 2, 1_000, 2),
            model_limits=ModelContextLimits(16_000, 1_000),
            workspace_root=tmp_path,
            permissions=PermissionProfile(),
            plc_subagents=PlcSubagentConfig(
                enabled=True,
                base_url="https://plc.example",
                timeout_secs=30,
            ),
            tool_runtime=registry,
        ),
        session,
        "create PLC code",
        handler_events,
    )

    assert outcome.error is None
    tool_messages = [
        message
        for message in model.requests[1].conversation.messages
        if message.tool_call_id == "plc-call"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].content is not None
    tool_output = json.loads(tool_messages[0].content)
    assert tool_output["ok"] is True
    assert tool_output["data"]["agent"] == "develop"
    assert tool_output["data"]["status"] == "generated_compiled"
    assert AgentEventType.TOOL_CALL_STARTED in handler_events.types
    assert AgentEventType.TOOL_CALL_FINISHED in handler_events.types

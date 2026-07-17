"""Remote PLC subagents exposed as normal Morrow tools."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Self, TypeVar, cast
from urllib.parse import urljoin

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from morrow.config import PlcSubagentConfig
from morrow.core import (
    ToolApproval,
    ToolExecution,
    ToolExecutionContext,
    ToolExecutionMode,
    ToolResult,
)
from morrow.protocol import ToolCall, ToolDefinition

from .builtins import TOOL_CANCELLED_ERROR

MAX_SSE_FRAME_BYTES = 4 * 1024 * 1024
MAX_HTTP_ERROR_BYTES = 64 * 1024
MAX_DIAGNOSTICS = 20
MAX_DIAGNOSTIC_CHARS = 2_000
MAX_FAILED_DETAILS = 20
MAX_FAILED_DETAIL_CHARS = 2_000

PLC_SUBAGENT_GUIDANCE = "\n".join(
    (
        "PLC subagent tool guidance:",
        "- Use plc_develop for new PLC code. It supports only ST and SCL.",
        "- Use plc_repair to diagnose compilation failures in existing ST code.",
        "- Pass complete ST source code to plc_test and plc_formal_verify.",
        "- For complete engineering tasks, normally proceed in this order: develop, test, "
        "then formal verification.",
        "- plc_repair is diagnostic-only and does not return fixed source code. Use its "
        "diagnostics to propose a change or apply one with the local file tools.",
        "- Test failures and failed formal properties are valid engineering findings, not "
        "tool invocation errors.",
        "- Do not repeat the same PLC tool call with unchanged code, requirements, or properties.",
    )
)

_REPORT_EVENTS = frozenset(
    {
        "st_code_json",
        "compilation_report_json",
        "formal_report_json",
        "fuzz_report_json",
    }
)
_ERROR_MARKERS = ("❌", "执行失败", "生成异常")
_Value = TypeVar("_Value")


class PlcSubagentError(RuntimeError):
    """Base class for a safe, user-facing PLC tool error."""


class PlcSubagentProtocolError(PlcSubagentError):
    """The remote stream did not satisfy the documented contract."""


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _DevelopInput(_StrictInput):
    requirement: str
    target_language: Literal["ST", "SCL"] = "ST"

    @field_validator("requirement")
    @classmethod
    def _require_requirement(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class _RepairInput(_StrictInput):
    st_code: str
    failure_notes: str | None = None

    @field_validator("st_code")
    @classmethod
    def _require_st_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _reject_null_failure_notes(self) -> Self:
        if "failure_notes" in self.model_fields_set and self.failure_notes is None:
            raise ValueError("failure_notes must be a string when provided")
        return self


class _FormalProperty(_StrictInput):
    description: str
    job_req: Literal["assertion", "pattern"]
    entry_point: str
    pattern_id: (
        Literal[
            "pattern-invariant",
            "pattern-implication",
            "pattern-forbidden",
        ]
        | None
    ) = None
    pattern_params: dict[str, str] | None = None

    @field_validator("description", "entry_point")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_pattern(self) -> Self:
        if self.job_req == "assertion":
            if {"pattern_id", "pattern_params"} & self.model_fields_set:
                raise ValueError("assertion properties must not include pattern fields")
            return self

        if self.pattern_id is None or self.pattern_params is None:
            raise ValueError("pattern properties require pattern_id and pattern_params")
        expected = {"1", "2"} if self.pattern_id == "pattern-implication" else {"1"}
        if set(self.pattern_params) != expected:
            joined = ", ".join(repr(key) for key in sorted(expected))
            raise ValueError(f"{self.pattern_id} requires exactly pattern parameter keys {joined}")
        if any(not value.strip() for value in self.pattern_params.values()):
            raise ValueError("pattern parameter values must not be empty")
        return self


class _FormalVerifyInput(_StrictInput):
    st_code: str
    properties: list[_FormalProperty] = Field(min_length=1, max_length=20)

    @field_validator("st_code")
    @classmethod
    def _require_st_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _require_assert_directive(self) -> Self:
        if (
            any(item.job_req == "assertion" for item in self.properties)
            and "//#ASSERT" not in self.st_code
        ):
            raise ValueError("assertion properties require the ST source to contain //#ASSERT")
        return self


class _TestInput(_StrictInput):
    st_code: str
    case_count: int = Field(default=10, ge=1, le=100)

    @field_validator("st_code")
    @classmethod
    def _require_st_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


@dataclass(frozen=True, slots=True)
class _RemoteResult:
    session_id: str
    report: dict[str, Any]
    diagnostics: list[str]
    stage_guidance: Any | None


class _SseDecoder:
    """Incrementally split SSE frames without assuming transport chunk boundaries."""

    def __init__(self, max_frame_bytes: int = MAX_SSE_FRAME_BYTES) -> None:
        self._max_frame_bytes = max_frame_bytes
        self._pending = bytearray()
        self._lines: list[bytes] = []
        self._frame_bytes = 0

    def feed(self, chunk: bytes) -> list[str]:
        if chunk:
            self._pending.extend(chunk)
        return self._drain(eof=False)

    def finish(self) -> list[str]:
        output = self._drain(eof=True)
        if self._lines:
            data = self._dispatch_frame()
            if data is not None:
                output.append(data)
        return output

    def _drain(self, *, eof: bool) -> list[str]:
        output: list[str] = []
        while self._pending:
            line_end = _find_line_end(self._pending, eof=eof)
            if line_end is None:
                if eof:
                    line = bytes(self._pending)
                    self._pending.clear()
                    self._push_line(line, 0, output)
                elif self._frame_bytes + len(self._pending) > self._max_frame_bytes:
                    raise PlcSubagentProtocolError(
                        f"SSE frame exceeds {self._max_frame_bytes} bytes"
                    )
                break
            index, terminator_bytes = line_end
            line = bytes(self._pending[:index])
            del self._pending[: index + terminator_bytes]
            self._push_line(line, terminator_bytes, output)
        return output

    def _push_line(self, line: bytes, terminator_bytes: int, output: list[str]) -> None:
        self._frame_bytes += len(line) + terminator_bytes
        if self._frame_bytes > self._max_frame_bytes:
            raise PlcSubagentProtocolError(f"SSE frame exceeds {self._max_frame_bytes} bytes")
        if line:
            self._lines.append(line)
            return
        data = self._dispatch_frame()
        if data is not None:
            output.append(data)

    def _dispatch_frame(self) -> str | None:
        lines, self._lines = self._lines, []
        self._frame_bytes = 0
        data_lines: list[bytes] = []
        for line in lines:
            if line.startswith(b":"):
                continue
            field, separator, value = line.partition(b":")
            if field != b"data":
                continue
            if separator and value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)
        if not data_lines:
            return None
        try:
            return b"\n".join(data_lines).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlcSubagentProtocolError("SSE data is not valid UTF-8") from exc


def _find_line_end(buffer: bytearray, *, eof: bool) -> tuple[int, int] | None:
    for index, value in enumerate(buffer):
        if value == 0x0A:
            return index, 1
        if value != 0x0D:
            continue
        if index + 1 < len(buffer):
            return index, 2 if buffer[index + 1] == 0x0A else 1
        if eof:
            return index, 1
        return None
    return None


class PlcSubagentTools:
    """Provider for the four stable PLC remote agents."""

    def __init__(
        self,
        config: PlcSubagentConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not config.enabled or config.base_url is None:
            raise ValueError("PLC subagent tools require an enabled configuration")
        self._config = config
        self._transport = transport
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def definitions(self) -> list[ToolDefinition]:
        return plc_subagent_definitions()

    def execution_mode(self, call: ToolCall) -> ToolExecutionMode:
        del call
        return ToolExecutionMode.SERIAL

    async def execute(
        self,
        call: ToolCall,
        approval: ToolApproval | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecution:
        del approval
        execution_context = context or ToolExecutionContext()
        if execution_context.cancellation.is_cancelled:
            return ToolExecution.error(TOOL_CANCELLED_ERROR)
        try:
            operation = self._dispatch(call)
            data = await _await_with_cancellation(operation, execution_context)
        except ValidationError as exc:
            return ToolExecution.error(
                f"invalid arguments for tool {call.function.name}: {_validation_detail(exc)}"
            )
        except PlcSubagentError as exc:
            return ToolExecution.error(str(exc))
        return ToolExecution.completed(_tool_ok(data))

    async def _dispatch(self, call: ToolCall) -> dict[str, Any]:
        name = call.function.name
        if name == "plc_develop":
            develop_args = _DevelopInput.model_validate_json(call.function.arguments)
            return await self._develop(develop_args)
        if name == "plc_repair":
            repair_args = _RepairInput.model_validate_json(call.function.arguments)
            return await self._repair(repair_args)
        if name == "plc_formal_verify":
            formal_args = _FormalVerifyInput.model_validate_json(call.function.arguments)
            return await self._formal_verify(formal_args)
        if name == "plc_test":
            test_args = _TestInput.model_validate_json(call.function.arguments)
            return await self._test(test_args)
        raise PlcSubagentError(f"unknown tool {name!r}")

    async def _develop(self, args: _DevelopInput) -> dict[str, Any]:
        remote = await self._request(
            message=args.requirement,
            agent_id="retrieval_planning_coding_agent",
            context={
                "target_language": args.target_language,
                "compiler_type": "matiec",
                "enable_socratic_spec": False,
            },
            expected_event="st_code_json",
        )
        event = remote.report
        st_code = event.get("stCode")
        content = event.get("content")
        containers = [item for item in (st_code, content, event) if isinstance(item, Mapping)]
        code = _first_string(containers, ("code", "st_code", "source"))
        if code is None or not code.strip():
            raise PlcSubagentProtocolError("st_code_json does not contain generated code")
        language = _first_string(containers, ("language",)) or args.target_language
        file_name = _first_string(containers, ("file_name", "filename"))
        if file_name is None:
            file_path = _first_string(containers, ("file_path", "path"))
            if file_path:
                file_name = PurePosixPath(file_path.replace("\\", "/")).name
        if file_name is None or not file_name:
            raise PlcSubagentProtocolError("st_code_json does not contain a file_name")
        compile_ok = _first_bool(
            containers,
            ("compile_ok", "compilation_success", "compile_success", "compiled"),
        )
        if compile_ok is None:
            compile_ok = not remote.diagnostics
        result: dict[str, Any] = {
            "code": code,
            "language": language,
            "file_name": file_name,
            "compile_ok": compile_ok,
        }
        _attach_stage_guidance(result, remote.stage_guidance)
        return self._normalized(
            agent="develop",
            status="generated_compiled" if compile_ok else "generated_uncompiled",
            remote=remote,
            result=result,
            artifacts=_artifact_references(event, self._config.base_url),
        )

    async def _repair(self, args: _RepairInput) -> dict[str, Any]:
        request_context: dict[str, Any] = {
            "repair_source": "compile",
            "compiler_type": "matiec",
        }
        if args.failure_notes is not None:
            request_context["repair_failure_notes"] = args.failure_notes
        remote = await self._request(
            message=_json_dumps({"st_code": args.st_code}),
            agent_id="compilation_debugging_agent",
            context=request_context,
            expected_event="compilation_report_json",
        )
        report = _report_content(remote.report, "compilation_report_json")
        compilation_success = _required_bool(report, "compilation_success")
        errors = report.get("errors", [])
        if not isinstance(errors, list):
            raise PlcSubagentProtocolError("compilation report errors must be an array")
        attempt_count = _optional_int(report.get("attempt_count"), "attempt_count")
        fix_summary = report.get("fix_summary")
        if fix_summary is None:
            fix_summary = report.get("fixes_applied", report.get("suggestions"))
        result = {
            "compilation_success": compilation_success,
            "code_file": _optional_string(report.get("code_file")),
            "report_id": _optional_string(report.get("report_id")),
            "errors": errors,
            "fix_summary": _concise_value(fix_summary),
            "attempt_count": attempt_count,
            "fixed_code": None,
            "fixed_code_note": (
                "The upstream compile-repair agent returns diagnostics and a remote file name "
                "but does not expose the repaired source code."
            ),
        }
        _attach_stage_guidance(result, remote.stage_guidance)
        return self._normalized(
            agent="repair",
            status="compiled" if compilation_success else "not_repaired",
            remote=remote,
            result=result,
            artifacts=_artifact_references(report, self._config.base_url),
        )

    async def _formal_verify(self, args: _FormalVerifyInput) -> dict[str, Any]:
        properties = []
        for item in args.properties:
            remote_property: dict[str, Any] = {
                "job_req": item.job_req,
                "entry_point": item.entry_point,
            }
            if item.job_req == "pattern":
                remote_property["pattern_id"] = item.pattern_id
                remote_property["pattern_params"] = item.pattern_params
            properties.append(
                {
                    "property_description": item.description,
                    "property": remote_property,
                }
            )
        remote = await self._request(
            message=_json_dumps({"st_code": args.st_code, "properties": properties}),
            agent_id="formal_validation_agent",
            context={},
            expected_event="formal_report_json",
        )
        report = _report_content(remote.report, "formal_report_json")
        raw_results = report.get("property_results")
        if not isinstance(raw_results, list) or not raw_results:
            raw_results = report.get("properties")
        if not isinstance(raw_results, list):
            raise PlcSubagentProtocolError("formal report properties must be an array")
        expected_count = len(args.properties)
        reported_count = report.get("property_count", len(raw_results))
        if not _is_int(reported_count):
            raise PlcSubagentProtocolError("formal report property_count must be an integer")
        if reported_count != expected_count or len(raw_results) != expected_count:
            raise PlcSubagentProtocolError(
                "formal report property count does not match the request "
                f"({reported_count} reported, {len(raw_results)} results, "
                f"{expected_count} requested)"
            )

        normalized_properties: list[dict[str, Any]] = []
        status_counts = {"PASS": 0, "FAIL": 0, "NOT_CHECKED": 0}
        for index, raw_item in enumerate(raw_results):
            if not isinstance(raw_item, Mapping):
                raise PlcSubagentProtocolError(
                    f"formal report property {index + 1} must be an object"
                )
            status = _formal_status(raw_item)
            status_counts[status] += 1
            requested = args.properties[index]
            normalized_properties.append(
                _normalize_formal_property(index, raw_item, requested, status)
            )

        passed = status_counts["PASS"]
        failed = status_counts["FAIL"]
        not_checked = status_counts["NOT_CHECKED"]
        if passed == expected_count:
            overall_status = "passed"
        elif failed == expected_count:
            overall_status = "failed"
        elif not_checked == expected_count:
            overall_status = "not_checked"
        else:
            overall_status = "mixed"
        backend = _concise_value(
            _first_present(report, ("backend", "backend_used", "verification_backend"))
        )
        if backend is None:
            property_backends = []
            for normalized_item in normalized_properties:
                item_backend = normalized_item["backend"]
                if item_backend is not None and item_backend not in property_backends:
                    property_backends.append(item_backend)
            if len(property_backends) == 1:
                backend = property_backends[0]
            elif property_backends:
                backend = property_backends
        result = {
            "report_id": _optional_string(report.get("report_id")),
            "property_count": expected_count,
            "passed": passed,
            "failed": failed,
            "not_checked": not_checked,
            "all_satisfied": passed == expected_count,
            "backend": backend,
            "summary": _concise_value(
                _first_present(report, ("summary", "message", "result_summary"))
            ),
            "counterexample_summary": _counterexample_summary(report.get("counterexample")),
            "verification_time_ms": _optional_number(report.get("verification_time_ms")),
            "properties": normalized_properties,
        }
        _attach_stage_guidance(result, remote.stage_guidance)
        return self._normalized(
            agent="formal_verify",
            status=overall_status,
            remote=remote,
            result=result,
            artifacts=_artifact_references(report, self._config.base_url),
        )

    async def _test(self, args: _TestInput) -> dict[str, Any]:
        remote = await self._request(
            message=_json_dumps({"st_code": args.st_code}),
            agent_id="fuzz_testing_agent",
            context={"fuzz_method": "legacy", "case_count": args.case_count},
            expected_event="fuzz_report_json",
        )
        report = _report_content(remote.report, "fuzz_report_json")
        summary = report.get("summary")
        summary_mapping = summary if isinstance(summary, Mapping) else {}
        total = _first_required_int(
            (summary_mapping, report),
            ("total_test_cases", "total_cases", "total"),
            "total test cases",
        )
        passed = _first_required_int(
            (summary_mapping, report),
            ("success_cases", "passed_cases", "passed"),
            "passed test cases",
        )
        failed = _first_required_int(
            (summary_mapping, report),
            ("failed_cases", "failed"),
            "failed test cases",
        )
        if min(total, passed, failed) < 0:
            raise PlcSubagentProtocolError("test report case counts must not be negative")
        remote_method = report.get("fuzz_method")
        if isinstance(remote_method, str) and remote_method.lower() != "legacy":
            raise PlcSubagentProtocolError(
                f"test report used unexpected fuzz method {remote_method!r}"
            )
        workflow_success = report.get("workflow_success", True)
        if not isinstance(workflow_success, bool):
            raise PlcSubagentProtocolError("test report workflow_success must be a boolean")
        counts_complete = passed + failed == total and total == args.case_count
        if workflow_success and counts_complete:
            status = "failed" if failed else "passed"
        else:
            status = "partial"

        raw_failed_details = report.get("failed_details", [])
        if not isinstance(raw_failed_details, list):
            raise PlcSubagentProtocolError("test report failed_details must be an array")
        failed_details = [
            _trim_failed_detail(item) for item in raw_failed_details[:MAX_FAILED_DETAILS]
        ]
        coverage = report.get("coverage_statistics", {})
        if not isinstance(coverage, Mapping):
            raise PlcSubagentProtocolError("test report coverage_statistics must be an object")
        success_rate = _first_present(
            summary_mapping,
            ("success_rate_pct", "pass_rate_pct", "success_rate"),
        )
        if not isinstance(success_rate, (int, float)) or isinstance(success_rate, bool):
            success_rate = round((passed / total) * 100, 3) if total else 0.0
        result = {
            "report_id": _optional_string(report.get("report_id")),
            "requested_case_count": args.case_count,
            "total_test_cases": total,
            "passed_cases": passed,
            "failed_cases": failed,
            "success_rate_pct": success_rate,
            "coverage_statistics": dict(coverage),
            "failed_details": failed_details,
            "failed_details_truncated": len(raw_failed_details) > MAX_FAILED_DETAILS
            or any(_detail_was_trimmed(item) for item in failed_details),
            "execution_backend": _optional_string(report.get("execution_backend")),
            "compile_backend": _optional_string(report.get("compile_backend")),
            "fuzz_method": "legacy",
        }
        _attach_stage_guidance(result, remote.stage_guidance)
        return self._normalized(
            agent="test",
            status=status,
            remote=remote,
            result=result,
            artifacts=_artifact_references(report, self._config.base_url),
        )

    async def _request(
        self,
        *,
        message: str,
        agent_id: str,
        context: dict[str, Any],
        expected_event: str,
    ) -> _RemoteResult:
        base_url = self._config.base_url
        assert base_url is not None
        payload = {
            "message": message,
            "agent_id": agent_id,
            "session_id": f"morrow_session_{self._id_factory()}",
            "user_id": f"morrow_user_{self._id_factory()}",
            "language": "zh-CN",
            "context": context,
            "uploaded_files": [],
        }
        try:
            async with asyncio.timeout(self._config.timeout_secs):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=httpx.Timeout(float(self._config.timeout_secs)),
                    follow_redirects=False,
                    headers={"Accept": "text/event-stream"},
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{base_url}/api/chat/stream",
                        json=payload,
                    ) as response:
                        if response.status_code < 200 or response.status_code >= 300:
                            body = await _read_limited_error_body(response)
                            detail = f": {body}" if body else ""
                            raise PlcSubagentError(
                                f"PLC subagent HTTP {response.status_code}{detail}"
                            )
                        content_type = response.headers.get("content-type", "")
                        media_type = content_type.split(";", 1)[0].strip().lower()
                        if media_type != "text/event-stream":
                            raise PlcSubagentProtocolError(
                                "PLC subagent response Content-Type must be text/event-stream, "
                                f"got {content_type or '<missing>'!r}"
                            )
                        return await _consume_sse(response, expected_event)
        except TimeoutError as exc:
            raise PlcSubagentError(
                f"PLC subagent request timed out after {self._config.timeout_secs} seconds"
            ) from exc
        except httpx.TimeoutException as exc:
            raise PlcSubagentError(
                f"PLC subagent request timed out after {self._config.timeout_secs} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise PlcSubagentError(f"PLC subagent network request failed: {exc}") from exc

    def _normalized(
        self,
        *,
        agent: str,
        status: str,
        remote: _RemoteResult,
        result: dict[str, Any],
        artifacts: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "agent": agent,
            "status": status,
            "remote_session_id": remote.session_id,
            "result": result,
            "diagnostics": remote.diagnostics,
            "artifacts": artifacts,
        }


async def _await_with_cancellation(
    operation: Coroutine[Any, Any, _Value], context: ToolExecutionContext
) -> _Value:
    task = asyncio.create_task(operation)
    cancelled = asyncio.create_task(context.cancellation.cancelled())
    try:
        done, _ = await asyncio.wait(
            {task, cancelled},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done and task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise PlcSubagentError(TOOL_CANCELLED_ERROR)
        return await task
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


async def _consume_sse(response: httpx.Response, expected_event: str) -> _RemoteResult:
    decoder = _SseDecoder()
    session_id: str | None = None
    report: dict[str, Any] | None = None
    diagnostics: list[str] = []
    stage_guidance: Any | None = None

    async for chunk in response.aiter_bytes():
        for data in decoder.feed(chunk):
            session_id, report, stage_guidance = _consume_event(
                data,
                expected_event,
                session_id,
                report,
                diagnostics,
                stage_guidance,
            )
    for data in decoder.finish():
        session_id, report, stage_guidance = _consume_event(
            data,
            expected_event,
            session_id,
            report,
            diagnostics,
            stage_guidance,
        )

    if session_id is None:
        raise PlcSubagentProtocolError("PLC subagent stream did not provide a session_id")
    if report is None:
        detail = f"; diagnostics: {diagnostics[0]}" if diagnostics else ""
        raise PlcSubagentProtocolError(
            f"PLC subagent stream did not provide {expected_event}{detail}"
        )
    return _RemoteResult(session_id, report, diagnostics, stage_guidance)


def _consume_event(
    data: str,
    expected_event: str,
    session_id: str | None,
    report: dict[str, Any] | None,
    diagnostics: list[str],
    stage_guidance: Any | None,
) -> tuple[str | None, dict[str, Any] | None, Any | None]:
    try:
        event = json.loads(data)
    except json.JSONDecodeError as exc:
        raise PlcSubagentProtocolError(f"malformed JSON in SSE event: {exc.msg}") from exc
    if not isinstance(event, dict):
        raise PlcSubagentProtocolError("SSE event JSON must be an object")
    event_type = event.get("type")
    if event_type == "session_id" and session_id is None:
        session_id = _extract_session_id(event)
    elif event_type == expected_event:
        if report is not None:
            raise PlcSubagentProtocolError(
                f"PLC subagent stream returned multiple {expected_event} events"
            )
        report = event
    elif isinstance(event_type, str) and event_type in _REPORT_EVENTS:
        raise PlcSubagentProtocolError(
            f"PLC subagent stream returned unexpected report event {event_type!r}"
        )
    elif event_type == "stage_guidance":
        stage_guidance = _concise_value(event.get("content", event.get("data")))

    if event_type == "error":
        _append_diagnostic(diagnostics, _event_text(event) or _json_dumps(event))
    elif event_type == "token":
        text = _event_text(event)
        if text and any(marker in text for marker in _ERROR_MARKERS):
            _append_diagnostic(diagnostics, text)
    return session_id, report, stage_guidance


def _extract_session_id(event: Mapping[str, Any]) -> str:
    candidates = [event.get("session_id"), event.get("content"), event.get("data")]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if isinstance(candidate, Mapping):
            value = candidate.get("session_id")
            if isinstance(value, str) and value.strip():
                return value
    raise PlcSubagentProtocolError("session_id event does not contain a session ID")


def _event_text(event: Mapping[str, Any]) -> str | None:
    for key in ("content", "message", "token", "text", "error", "detail"):
        value = event.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for nested_key in ("message", "content", "text", "error", "detail"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    return nested
    return None


def _append_diagnostic(diagnostics: list[str], value: str) -> None:
    if len(diagnostics) >= MAX_DIAGNOSTICS:
        return
    normalized = value.strip()
    if not normalized:
        return
    diagnostics.append(normalized[:MAX_DIAGNOSTIC_CHARS])


async def _read_limited_error_body(response: httpx.Response) -> str:
    body = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = MAX_HTTP_ERROR_BYTES - len(body)
        if remaining > 0:
            body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    text = bytes(body).decode("utf-8", errors="replace").strip()
    return f"{text}…" if truncated else text


def plc_subagent_definitions() -> list[ToolDefinition]:
    pattern_params_one = {
        "type": "object",
        "properties": {"1": {"type": "string", "minLength": 1}},
        "required": ["1"],
        "additionalProperties": False,
    }
    pattern_params_two = {
        "type": "object",
        "properties": {
            "1": {"type": "string", "minLength": 1},
            "2": {"type": "string", "minLength": 1},
        },
        "required": ["1", "2"],
        "additionalProperties": False,
    }
    base_property = {
        "description": {"type": "string", "minLength": 1},
        "entry_point": {"type": "string", "minLength": 1},
    }
    property_variants = [
        {
            "type": "object",
            "properties": {
                **base_property,
                "job_req": {"const": "assertion"},
            },
            "required": ["description", "job_req", "entry_point"],
            "additionalProperties": False,
        }
    ]
    for pattern_id, params in (
        ("pattern-invariant", pattern_params_one),
        ("pattern-forbidden", pattern_params_one),
        ("pattern-implication", pattern_params_two),
    ):
        property_variants.append(
            {
                "type": "object",
                "properties": {
                    **base_property,
                    "job_req": {"const": "pattern"},
                    "pattern_id": {"const": pattern_id},
                    "pattern_params": params,
                },
                "required": [
                    "description",
                    "job_req",
                    "entry_point",
                    "pattern_id",
                    "pattern_params",
                ],
                "additionalProperties": False,
            }
        )
    return [
        ToolDefinition.function(
            "plc_develop",
            "Generate PLC source code for a new requirement using the remote PLC developer.",
            {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string", "minLength": 1},
                    "target_language": {
                        "type": "string",
                        "enum": ["ST", "SCL"],
                        "default": "ST",
                    },
                },
                "required": ["requirement"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "plc_repair",
            "Diagnose compilation failures in complete ST source. This tool does not "
            "return fixed source code.",
            {
                "type": "object",
                "properties": {
                    "st_code": {"type": "string", "minLength": 1},
                    "failure_notes": {"type": "string"},
                },
                "required": ["st_code"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "plc_formal_verify",
            "Formally verify one to twenty standard properties against complete ST source.",
            {
                "type": "object",
                "properties": {
                    "st_code": {"type": "string", "minLength": 1},
                    "properties": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {"oneOf": property_variants},
                    },
                },
                "required": ["st_code", "properties"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition.function(
            "plc_test",
            "Run the stable legacy PLC test workflow against complete ST source.",
            {
                "type": "object",
                "properties": {
                    "st_code": {"type": "string", "minLength": 1},
                    "case_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 10,
                    },
                },
                "required": ["st_code"],
                "additionalProperties": False,
            },
        ),
    ]


def _report_content(event: Mapping[str, Any], event_name: str) -> dict[str, Any]:
    for key in ("content", "data"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    raise PlcSubagentProtocolError(f"{event_name} does not contain an object report")


def _normalize_formal_property(
    index: int,
    raw: Mapping[str, Any],
    requested: _FormalProperty,
    status: str,
) -> dict[str, Any]:
    containers = [raw]
    for key in ("property", "result", "verification_result"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    summary_value = _first_present_from(containers, ("summary", "message", "reason", "details"))
    summary_mapping = _json_object(summary_value)
    backend_value = _first_present_from(
        containers,
        ("backend", "backend_used", "verification_backend", "checker"),
    )
    if backend_value is None and summary_mapping is not None:
        backend_value = _first_present(
            summary_mapping,
            ("backend", "backend_used", "verification_backend", "checker"),
        )
    return {
        "index": index + 1,
        "description": _first_string(containers, ("property_description", "description"))
        or requested.description,
        "status": status,
        "job_req": _first_string(containers, ("job_req",)) or requested.job_req,
        "entry_point": _first_string(containers, ("entry_point",)) or requested.entry_point,
        "pattern_id": _first_string(containers, ("pattern_id",)) or requested.pattern_id,
        "pattern_params": _first_present_from(containers, ("pattern_params",))
        or requested.pattern_params,
        "backend": _concise_value(backend_value),
        "summary": _concise_value(summary_mapping or summary_value),
        "counterexample_summary": _counterexample_summary(
            _first_present_from(containers, ("counterexample", "counter_example", "trace"))
        ),
    }


def _formal_status(raw: Mapping[str, Any]) -> Literal["PASS", "FAIL", "NOT_CHECKED"]:
    containers = [raw]
    for key in ("result", "verification_result"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    value = _first_present_from(containers, ("status", "verification_status"))
    if value is None:
        satisfied = _first_present_from(containers, ("satisfied", "passed"))
        if isinstance(satisfied, bool):
            return "PASS" if satisfied else "FAIL"
    if not isinstance(value, str):
        raise PlcSubagentProtocolError("formal property result does not contain a status")
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"PASS", "PASSED", "SATISFIED", "SUCCESS", "TRUE"}:
        return "PASS"
    if normalized in {"FAIL", "FAILED", "VIOLATED", "UNSATISFIED", "FALSE"}:
        return "FAIL"
    if normalized in {
        "NOT_CHECKED",
        "UNKNOWN",
        "ERROR",
        "TIMEOUT",
        "SKIPPED",
        "NOT_RUN",
    }:
        return "NOT_CHECKED"
    raise PlcSubagentProtocolError(f"unknown formal property status {value!r}")


def _artifact_references(report: Mapping[str, Any], base_url: str | None) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def visit(value: Any, prefix: str = "") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(nested, str) and (
                    str(key).lower().endswith("url") or str(key).lower() == "url"
                ):
                    add(name, nested)
                elif isinstance(nested, Mapping):
                    visit(nested, name)

    def add(name: str, value: str) -> None:
        if not value.strip():
            return
        url = urljoin(f"{base_url}/", value) if base_url is not None else value
        item = (name, url)
        if item in seen:
            return
        seen.add(item)
        references.append({"name": name, "url": url})

    artifacts = report.get("artifacts")
    if isinstance(artifacts, Mapping):
        visit(artifacts)
    for key, value in report.items():
        if isinstance(value, str) and (
            str(key).lower().endswith("url") or str(key).lower() == "url"
        ):
            add(str(key), value)
    return references


def _counterexample_summary(value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in ("summary", "description", "message", "reason", "trace_summary"):
            candidate = value.get(key)
            if candidate is not None:
                return _concise_value(candidate)
    return _concise_value(value)


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _concise_value(value: Any, max_chars: int = MAX_DIAGNOSTIC_CHARS) -> Any | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_chars]
    try:
        encoded = _json_dumps(value)
    except (TypeError, ValueError):
        encoded = str(value)
    if len(encoded) <= max_chars:
        return value
    return {"summary": encoded[:max_chars], "truncated": True}


def _trim_failed_detail(value: Any) -> Any:
    concise = _concise_value(value, MAX_FAILED_DETAIL_CHARS)
    return concise


def _detail_was_trimmed(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("truncated") is True


def _attach_stage_guidance(result: dict[str, Any], guidance: Any | None) -> None:
    if guidance is not None:
        result["stage_guidance"] = guidance


def _validation_detail(error: ValidationError) -> str:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if not details:
        return "invalid input"
    first = details[0]
    location = ".".join(str(part) for part in first["loc"])
    message = str(first["msg"])
    return f"{location}: {message}" if location else message


def _first_string(
    containers: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    names: tuple[str, ...],
) -> str | None:
    value = _first_present_from(containers, names)
    return value if isinstance(value, str) else None


def _first_bool(containers: list[Mapping[str, Any]], names: tuple[str, ...]) -> bool | None:
    value = _first_present_from(containers, names)
    return value if isinstance(value, bool) else None


def _first_present(container: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in container:
            return container[name]
    return None


def _first_present_from(
    containers: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    names: tuple[str, ...],
) -> Any:
    for container in containers:
        value = _first_present(container, names)
        if value is not None:
            return value
    return None


def _first_required_int(
    containers: tuple[Mapping[str, Any], ...],
    names: tuple[str, ...],
    label: str,
) -> int:
    value = _first_present_from(containers, names)
    if not _is_int(value):
        raise PlcSubagentProtocolError(f"test report {label} must be an integer")
    return cast(int, value)


def _required_bool(container: Mapping[str, Any], name: str) -> bool:
    value = container.get(name)
    if not isinstance(value, bool):
        raise PlcSubagentProtocolError(f"report field {name} must be a boolean")
    return value


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not _is_int(value):
        raise PlcSubagentProtocolError(f"report field {name} must be an integer")
    return cast(int, value)


def _optional_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return cast(int | float, value)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _tool_ok(data: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=True, content=_json_dumps({"ok": True, "data": data}))


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "MAX_DIAGNOSTICS",
    "MAX_DIAGNOSTIC_CHARS",
    "MAX_HTTP_ERROR_BYTES",
    "MAX_SSE_FRAME_BYTES",
    "PLC_SUBAGENT_GUIDANCE",
    "PlcSubagentError",
    "PlcSubagentProtocolError",
    "PlcSubagentTools",
    "plc_subagent_definitions",
]

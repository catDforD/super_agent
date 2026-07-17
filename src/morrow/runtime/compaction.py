"""Session context estimation and loss-aware compaction."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from morrow.config import ContextConfig, ModelContextLimits
from morrow.core import Model, ModelEventType, ModelRequest
from morrow.protocol import (
    Conversation,
    Message,
    Role,
    Session,
    ToolDefinition,
    TurnRecord,
    TurnStatus,
)

MESSAGE_BASE_TOKENS = 6
TOOL_CALL_BASE_TOKENS = 12
REQUEST_PADDING_NUMERATOR = 4
REQUEST_PADDING_DENOMINATOR = 3
REQUIRED_SUMMARY_SECTIONS = (
    "User Goals and Constraints",
    "Important Decisions",
    "Files and Code State",
    "Commands, Results, and Errors",
    "Current Progress",
    "Pending Tasks",
    "Open Questions",
)


class CompactionError(RuntimeError):
    """Automatic or manual session compaction could not satisfy its contract."""


class CompactionOutcome(StrEnum):
    CHANGED = "changed"
    NOOP = "noop"


async def maybe_auto_compact(
    client: Model,
    system_prompt: str,
    session: Session,
    context_config: ContextConfig,
    model_limits: ModelContextLimits,
    prompt: str,
) -> None:
    await maybe_auto_compact_with_tools(
        client,
        system_prompt,
        session,
        context_config,
        model_limits,
        prompt,
        (),
    )


async def maybe_auto_compact_with_tools(
    client: Model,
    system_prompt: str,
    session: Session,
    context_config: ContextConfig,
    model_limits: ModelContextLimits,
    prompt: str,
    tools: Sequence[ToolDefinition],
) -> None:
    if not context_config.auto_compact:
        return

    budget = auto_compact_trigger_tokens(model_limits, context_config)
    estimate = estimate_context_tokens(system_prompt, session, prompt, tools)
    if estimate <= budget:
        return

    await compact_session(client, session, context_config)
    compacted_estimate = estimate_context_tokens(system_prompt, session, prompt, tools)
    if compacted_estimate > budget:
        raise CompactionError(
            f"context is still over token budget after compaction ({compacted_estimate} > {budget})"
        )


def auto_compact_trigger_tokens(
    model_limits: ModelContextLimits,
    context_config: ContextConfig,
) -> int:
    input_window = max(
        0,
        model_limits.context_window_tokens - model_limits.reserved_output_tokens,
    )
    return int(input_window * context_config.auto_compact_threshold)


async def compact_session(
    client: Model,
    session: Session,
    context_config: ContextConfig,
) -> CompactionOutcome:
    prefix_len = compactable_prefix_len(session, context_config.retain_recent_turns)
    if prefix_len <= session.context.summarized_turns:
        return CompactionOutcome.NOOP

    first_turn_index = session.context.summarized_turns
    records = [
        record.model_copy(deep=True) for record in session.turns[first_turn_index:prefix_len]
    ]
    summary = await request_session_summary(
        client,
        session.context.summary,
        context_config.summary_target_tokens,
        context_config.compact_max_retries,
        records,
        first_turn_index,
    )
    session.context.summary = summary
    session.context.summarized_turns = prefix_len
    rebuild_active_thread(session)
    return CompactionOutcome.CHANGED


def rebuild_active_thread(session: Session) -> None:
    messages: list[Message] = []
    if session.context.summary is not None:
        messages.append(Message.system(f"Session summary:\n{session.context.summary}"))
    for record in session.turns[session.context.summarized_turns :]:
        if record.turn.status is TurnStatus.COMPLETED:
            messages.extend(message.model_copy(deep=True) for message in record.messages)
    session.active_thread.messages = messages


def compactable_prefix_len(session: Session, retain_recent_turns: int) -> int:
    completed_indices = [
        index
        for index, record in enumerate(session.turns)
        if record.turn.status is TurnStatus.COMPLETED
    ]
    if len(completed_indices) <= retain_recent_turns:
        return session.context.summarized_turns
    if retain_recent_turns == 0:
        return len(session.turns)
    boundary = completed_indices[len(completed_indices) - retain_recent_turns]
    return max(boundary, session.context.summarized_turns)


async def request_session_summary(
    client: Model,
    existing_summary: str | None,
    target_tokens: int,
    max_attempts: int,
    records: Sequence[TurnRecord],
    first_turn_index: int,
) -> str:
    repair_feedback: str | None = None
    for _attempt in range(max(1, max_attempts)):
        try:
            output = await request_raw_session_summary(
                client,
                existing_summary,
                target_tokens,
                repair_feedback,
                records,
                first_turn_index,
            )
        except Exception:
            return deterministic_session_summary(
                existing_summary,
                records,
                first_turn_index,
            )
        try:
            return parse_compact_summary_output(output)
        except ValueError as error:
            repair_feedback = str(error)

    return deterministic_session_summary(existing_summary, records, first_turn_index)


async def request_raw_session_summary(
    client: Model,
    existing_summary: str | None,
    target_tokens: int,
    repair_feedback: str | None,
    records: Sequence[TurnRecord],
    first_turn_index: int,
) -> str:
    conversation = Conversation.with_system_prompt(
        "You compact long-running coding agent session history. Respond with text only. "
        "Do not call tools. Return one <analysis> block followed by one <summary> block."
    )
    conversation.push(
        Message.user(
            build_summary_prompt(
                existing_summary,
                target_tokens,
                repair_feedback,
                records,
                first_turn_index,
            )
        )
    )
    stream_or_awaitable = client.stream(ModelRequest(conversation=conversation, tools=[]))
    if inspect.isawaitable(stream_or_awaitable):
        stream_or_awaitable = await stream_or_awaitable
    stream = stream_or_awaitable
    output: list[str] = []
    async for event in stream:
        if event.type is ModelEventType.TEXT_DELTA:
            output.append(event.text)
        elif event.type is ModelEventType.COMPLETED:
            result = "".join(output).strip()
            if not result:
                raise CompactionError("summary model returned an empty summary")
            return result
        elif event.type is ModelEventType.TOOL_CALLS:
            raise CompactionError("summary model requested tool calls")
    raise CompactionError("summary model stream ended before completion")


def build_summary_prompt(
    existing_summary: str | None,
    target_tokens: int,
    repair_feedback: str | None,
    records: Sequence[TurnRecord],
    first_turn_index: int,
) -> str:
    lines = [
        f"Update the session summary. Target length: at most {target_tokens} tokens.",
        "Output exactly one <analysis> block followed by one <summary> block.",
        "The <summary> block must contain these section headings exactly:",
        *(f"- {section}" for section in REQUIRED_SUMMARY_SECTIONS),
        "",
        "Preserve user goals, constraints, decisions, file paths, code state, commands, "
        "results, errors, pending tasks, and open questions. Do not continue the conversation.",
    ]
    if repair_feedback and repair_feedback.strip():
        lines.extend(
            [
                "",
                "Repair feedback from the previous invalid compact output:",
                repair_feedback,
            ]
        )
    lines.extend(
        [
            "",
            "Existing summary:",
            existing_summary if existing_summary is not None else "(none)",
            "",
            "Turns to incorporate:",
        ]
    )
    for offset, record in enumerate(records):
        lines.append(turn_record_transcript(first_turn_index + offset, record))
    return "\n".join(lines) + "\n"


def turn_record_transcript(index: int, record: TurnRecord) -> str:
    lines = ["", f"Turn {index}: status={record.turn.status.value}"]
    if record.turn.error is not None:
        lines.append(f"turn_error: {record.turn.error}")
    for message in record.messages:
        lines.append(f"{message.role.value}:")
        if message.content is not None:
            lines.append(message.content)
        if message.tool_calls is not None:
            lines.append(f"tool_calls: {_json_wire(message.tool_calls)}")
        if message.tool_call_id is not None:
            lines.append(f"tool_call_id: {message.tool_call_id}")
    return "\n".join(lines)


def parse_compact_summary_output(output: str) -> str:
    normalized = strip_outer_markdown_code_fence(output)
    summary = extract_xml_block(normalized, "summary")
    if summary is None:
        raise ValueError("compact response missing <summary> block")
    summary = summary.strip()
    if not summary:
        raise ValueError("compact summary response was empty")
    for section in REQUIRED_SUMMARY_SECTIONS:
        if section not in summary:
            raise ValueError(f"compact summary missing required section: {section}")
    return summary


def extract_xml_block(content: str, tag: str) -> str | None:
    opening = re.search(rf"<{re.escape(tag)}(?:\s[^>]*)?>", content, flags=re.IGNORECASE)
    if opening is None:
        return None
    closing = re.search(
        rf"</{re.escape(tag)}\s*>",
        content[opening.end() :],
        flags=re.IGNORECASE,
    )
    if closing is None:
        raise ValueError(f"compact response missing closing </{tag}> tag")
    close_start = opening.end() + closing.start()
    return content[opening.end() : close_start].strip()


def strip_outer_markdown_code_fence(content: str) -> str:
    current = content.strip()
    while True:
        stripped = strip_markdown_code_fence(current)
        if stripped == current:
            return current
        current = stripped


def strip_markdown_code_fence(content: str) -> str:
    trimmed = content.strip()
    if not trimmed.startswith("```"):
        return trimmed
    lines = trimmed.splitlines()
    if not lines or not lines[0].lstrip().startswith("```"):
        return trimmed
    body = "\n".join(lines[1:]).rstrip()
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()


def deterministic_session_summary(
    existing_summary: str | None,
    records: Sequence[TurnRecord],
    first_turn_index: int,
) -> str:
    lines = [
        "User Goals and Constraints",
        "- Previous summary: "
        + (
            truncate_summary_text(existing_summary, 1_200)
            if existing_summary is not None
            else "(none)"
        ),
        f"- Compacted {len(records)} turn records with deterministic fallback.",
        "",
        "Important Decisions",
        "- (unknown from deterministic fallback)",
        "",
        "Files and Code State",
        "- (unknown from deterministic fallback)",
        "",
        "Commands, Results, and Errors",
    ]
    errors = [
        f"- Turn {first_turn_index + offset} error: {truncate_summary_text(record.turn.error, 320)}"
        for offset, record in enumerate(records)
        if record.turn.error is not None
    ]
    lines.extend(errors or ["- (none recorded)"])
    lines.extend(["", "Current Progress"])
    start = max(0, len(records) - 6)
    for offset in range(start, len(records)):
        record = records[offset]
        lines.append(f"- Turn {first_turn_index + offset}: status={record.turn.status.value}")
        if record.turn.user_message.content is not None:
            lines.append("  user: " + truncate_summary_text(record.turn.user_message.content, 240))
        assistant = record.turn.assistant_message
        if assistant is not None and assistant.content is not None:
            lines.append("  assistant: " + truncate_summary_text(assistant.content, 240))
    lines.extend(
        [
            "",
            "Pending Tasks",
            "- (unknown from deterministic fallback)",
            "",
            "Open Questions",
            "- (unknown from deterministic fallback)",
        ]
    )
    return "\n".join(lines).strip()


def truncate_summary_text(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content.strip()
    return content[:max_chars] + "..."


def estimate_context_tokens(
    system_prompt: str,
    session: Session,
    prompt: str,
    tools: Sequence[ToolDefinition] = (),
) -> int:
    tool_tokens = estimate_text_tokens(_json_wire(tools)) if tools else 0
    raw_total = (
        message_text_tokens(Role.SYSTEM, system_prompt)
        + message_text_tokens(Role.USER, prompt)
        + tool_tokens
        + sum(message_context_tokens(message) for message in session.active_thread.messages)
    )
    return (
        raw_total * REQUEST_PADDING_NUMERATOR + REQUEST_PADDING_DENOMINATOR - 1
    ) // REQUEST_PADDING_DENOMINATOR


def message_context_tokens(message: Message) -> int:
    total = MESSAGE_BASE_TOKENS + estimate_text_tokens(message.role.value)
    if message.content is not None:
        total += estimate_text_tokens(message.content)
    if message.tool_call_id is not None:
        total += estimate_text_tokens(message.tool_call_id)
    if message.tool_calls is not None:
        total += TOOL_CALL_BASE_TOKENS + estimate_text_tokens(_json_wire(message.tool_calls))
    return total


def message_text_tokens(role: Role, content: str) -> int:
    return MESSAGE_BASE_TOKENS + estimate_text_tokens(role.value) + estimate_text_tokens(content)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(character.isascii() for character in text)
    non_ascii_tokens = len(text) - ascii_chars
    return (ascii_chars + 3) // 4 + non_ascii_tokens


def _json_wire(value: Any) -> str:
    def convert(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", by_alias=True)
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [convert(child) for child in item]
        return item

    return json.dumps(convert(value), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "CompactionError",
    "CompactionOutcome",
    "REQUIRED_SUMMARY_SECTIONS",
    "auto_compact_trigger_tokens",
    "build_summary_prompt",
    "compact_session",
    "compactable_prefix_len",
    "deterministic_session_summary",
    "estimate_context_tokens",
    "estimate_text_tokens",
    "extract_xml_block",
    "maybe_auto_compact",
    "maybe_auto_compact_with_tools",
    "message_context_tokens",
    "parse_compact_summary_output",
    "rebuild_active_thread",
    "request_session_summary",
    "strip_outer_markdown_code_fence",
]

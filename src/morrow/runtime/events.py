"""Stable envelopes for CLI JSONL and WebSocket agent events."""

from __future__ import annotations

import time
from pathlib import Path

from morrow.protocol import AgentEvent, ProtocolModel

EVENT_SCHEMA_VERSION = 1


class AgentEventEnvelope(ProtocolModel):
    schema_version: int
    timestamp_ms: int
    session: str
    workspace_root: str
    turn_index: int
    event_index: int
    event: AgentEvent


def make_event_envelope(
    session_name: str,
    workspace_root: str | Path,
    turn_index: int,
    event_index: int,
    event: AgentEvent,
) -> AgentEventEnvelope:
    return AgentEventEnvelope(
        schema_version=EVENT_SCHEMA_VERSION,
        timestamp_ms=timestamp_ms(),
        session=session_name,
        workspace_root=str(workspace_root),
        turn_index=turn_index,
        event_index=event_index,
        event=event,
    )


def timestamp_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "AgentEventEnvelope",
    "make_event_envelope",
    "timestamp_ms",
]

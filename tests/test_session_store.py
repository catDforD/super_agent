from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from morrow.protocol import (
    SESSION_DOCUMENT_SCHEMA_VERSION,
    Message,
    Session,
    SessionDocument,
    Thread,
)
from morrow.runtime.session_store import (
    InvalidSessionName,
    SessionNotFound,
    SessionParseError,
    SessionStore,
    TargetExists,
    UnsupportedSchemaVersion,
)

GOLDEN_FIXTURES = Path(__file__).parent / "fixtures" / "golden"


def _golden_bytes(name: str) -> bytes:
    return (GOLDEN_FIXTURES / name).read_bytes().removesuffix(b"\n")


def _store(tmp_path: Path, cwd: Path, name: str = "default") -> SessionStore:
    return SessionStore(tmp_path / "sessions", tmp_path / "threads", cwd, name)


def _sample_session() -> Session:
    return Session.from_thread(Thread(messages=[Message.user("Hello"), Message.assistant("Hi")]))


def test_scope_is_canonical_cwd_hex_and_missing_load_is_empty(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)

    assert store.scope == os.fsencode(os.fspath(cwd.resolve())).hex()
    assert store.path == tmp_path / "sessions" / store.scope / "default.json"
    assert store.load() == Session.new()
    with pytest.raises(SessionNotFound):
        store.load_existing()


def test_rejects_invalid_session_names(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    for name in ("", "../escape", "a/b", "with.dot", "space name", "中文"):
        with pytest.raises(InvalidSessionName):
            _store(tmp_path, cwd, name)


def test_save_is_atomic_v3_and_round_trips(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)
    session = _sample_session()

    store.save(session)

    assert store.load() == session
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["schema_version"] == SESSION_DOCUMENT_SCHEMA_VERSION
    assert document["session"]["active_thread"]["messages"][0]["content"] == "Hello"
    assert not store.path.with_name(f"{store.path.name}.tmp-{os.getpid()}").exists()


def test_v3_serialization_matches_rust_pretty_json_golden(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)
    expected = _golden_bytes("session_v3.json")
    document = SessionDocument.model_validate(json.loads(expected))

    store.save(document.session)

    assert store.path.read_bytes() == expected
    assert store.export_document_bytes() == expected
    assert store.load() == document.session


@pytest.mark.parametrize("version", [1, 2])
def test_loads_legacy_v1_and_v2_thread_documents(tmp_path: Path, version: int) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)
    store.legacy_path.parent.mkdir(parents=True)
    store.legacy_path.write_bytes(_golden_bytes(f"session_v{version}.json"))

    assert store.load() == _sample_session()
    store.save(store.load())
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == 3
    assert store.legacy_path.is_file()


def test_primary_file_wins_and_parse_errors_preserve_input(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not-json", encoding="utf-8")
    store.legacy_path.parent.mkdir(parents=True)
    store.legacy_path.write_text(
        json.dumps({"schema_version": 2, "thread": {"messages": []}}),
        encoding="utf-8",
    )

    with pytest.raises(SessionParseError):
        store.load()
    assert store.path.read_text(encoding="utf-8") == "{not-json"


def test_unsupported_schema_is_reported_and_preserved(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)
    store.path.parent.mkdir(parents=True)
    content = json.dumps({"schema_version": 999, "session": {}})
    store.path.write_text(content, encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersion) as raised:
        store.load()
    assert raised.value.version == 999
    assert store.path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    "document, missing_field",
    [
        ({"schema_version": 1, "thread": {}}, "thread.messages"),
        ({"schema_version": 2, "thread": {}}, "thread.messages"),
        ({"schema_version": 3, "session": {}}, "session.active_thread"),
        (
            {
                "schema_version": 3,
                "session": {
                    "active_thread": {"messages": []},
                    "turns": [],
                    "context": {},
                },
            },
            "session.context.summarized_turns",
        ),
    ],
)
def test_rejects_documents_missing_rust_required_fields(
    tmp_path: Path,
    document: dict[str, object],
    missing_field: str,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    store = _store(tmp_path, cwd)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SessionParseError, match=missing_field):
        store.load()


def test_list_delete_rename_and_export(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    work = _store(tmp_path, cwd, "work")
    default = _store(tmp_path, cwd)
    work.save(_sample_session())
    default.save(Session.new())

    entries = default.list_current_scope()
    assert [entry.name for entry in entries] == ["default", "work"]
    assert entries[1].active_messages == 2

    exported = json.loads(work.export_document_bytes())
    assert SessionDocument.model_validate(exported).schema_version == 3

    renamed = work.rename("renamed")
    assert not work.path.exists()
    assert renamed.load() == _sample_session()

    with pytest.raises(TargetExists):
        default.rename("renamed")
    renamed.delete()
    with pytest.raises(SessionNotFound):
        renamed.delete()

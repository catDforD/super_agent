"""Project-scoped, versioned session persistence."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from pydantic import ValidationError

from morrow.protocol import (
    SESSION_DOCUMENT_SCHEMA_VERSION,
    THREAD_DOCUMENT_SCHEMA_VERSION,
    Session,
    SessionDocument,
    ThreadDocument,
)


class SessionStoreError(Exception):
    """Base class for session persistence errors."""


class HomeDirNotFound(SessionStoreError):
    def __init__(self) -> None:
        super().__init__("home directory was not found")


class CurrentDirError(SessionStoreError):
    def __init__(self, source: BaseException) -> None:
        self.source = source
        super().__init__(f"failed to read current working directory: {source}")


class CanonicalizeCwdError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to canonicalize current working directory {path}: {source}")


class InvalidSessionName(SessionStoreError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"invalid session name {name!r}; use ASCII letters, digits, '-' or '_'")


class SessionReadError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to read session file {path}: {source}")


class SessionParseError(SessionStoreError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"failed to parse session file {path}: {detail}")


class SessionNotFound(SessionStoreError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"session {name!r} was not found")


class UnsupportedSchemaVersion(SessionStoreError):
    def __init__(self, path: Path, version: int, expected: int) -> None:
        self.path = path
        self.version = version
        self.expected = expected
        super().__init__(
            f"unsupported session document schema version {version} in {path}; expected {expected}"
        )


class SessionCreateDirError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to create session directory {path}: {source}")


class SessionSerializeError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to serialize session file {path}: {source}")


class SessionWriteError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to write session file {path}: {source}")


class SessionReplaceError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to replace session file {path}: {source}")


class SessionListError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to list session directory {path}: {source}")


class SessionMetadataError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to read session metadata {path}: {source}")


class SessionRemoveError(SessionStoreError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to remove session file {path}: {source}")


class TargetExists(SessionStoreError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"target session already exists at {path}")


class SessionEntry:
    """Metadata returned by :meth:`SessionStore.list_current_scope`."""

    __slots__ = (
        "active_messages",
        "has_summary",
        "name",
        "path",
        "summarized_turns",
        "turns",
    )

    def __init__(
        self,
        *,
        name: str,
        path: Path,
        turns: int,
        active_messages: int,
        summarized_turns: int,
        has_summary: bool,
    ) -> None:
        self.name = name
        self.path = path
        self.turns = turns
        self.active_messages = active_messages
        self.summarized_turns = summarized_turns
        self.has_summary = has_summary

    def __repr__(self) -> str:
        return (
            f"SessionEntry(name={self.name!r}, path={self.path!r}, turns={self.turns!r}, "
            f"active_messages={self.active_messages!r}, "
            f"summarized_turns={self.summarized_turns!r}, "
            f"has_summary={self.has_summary!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionEntry):
            return NotImplemented
        return all(getattr(self, field) == getattr(other, field) for field in self.__slots__)


class SessionStore:
    def __init__(
        self,
        root: str | Path,
        legacy_root: str | Path,
        cwd: str | Path,
        session_name: str,
    ) -> None:
        validate_session_name(session_name)
        self._root = Path(root)
        self._legacy_root = Path(legacy_root)
        cwd_path = Path(cwd)
        try:
            canonical_cwd = cwd_path.resolve(strict=True)
        except OSError as exc:
            raise CanonicalizeCwdError(cwd_path, exc) from exc
        self._scope = os.fsencode(os.fspath(canonical_cwd)).hex()
        self._session_name = session_name
        file_name = f"{session_name}.json"
        self._path = self._root / self._scope / file_name
        self._legacy_path = self._legacy_root / self._scope / file_name

    @classmethod
    def for_current_dir(cls, session_name: str) -> SessionStore:
        try:
            home = Path.home()
        except RuntimeError as exc:
            raise HomeDirNotFound from exc
        try:
            cwd = Path.cwd()
        except OSError as exc:
            raise CurrentDirError(exc) from exc
        return cls.for_workspace(cwd, session_name, home=home)

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        session_name: str,
        *,
        home: str | Path | None = None,
    ) -> SessionStore:
        if home is None:
            try:
                resolved_home = Path.home()
            except RuntimeError as exc:
                raise HomeDirNotFound from exc
        else:
            resolved_home = Path(home)
        morrow_home = resolved_home / ".morrow"
        return cls(
            morrow_home / "sessions",
            morrow_home / "threads",
            workspace,
            session_name,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def legacy_path(self) -> Path:
        return self._legacy_path

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def session_name(self) -> str:
        return self._session_name

    def load(self) -> Session:
        if self._path.is_file():
            return self._load_path(self._path)
        if self._legacy_path.is_file():
            return self._load_path(self._legacy_path)
        return Session.new()

    def load_existing(self) -> Session:
        if self._path.is_file():
            return self._load_path(self._path)
        if self._legacy_path.is_file():
            return self._load_path(self._legacy_path)
        raise SessionNotFound(self._session_name)

    def save(self, session: Session) -> None:
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionCreateDirError(parent, exc) from exc

        content = _serialize_document(SessionDocument.new(session), self._path)
        temp_path = self._temp_path()
        try:
            temp_path.write_bytes(content)
        except OSError as exc:
            raise SessionWriteError(temp_path, exc) from exc
        try:
            os.replace(temp_path, self._path)
        except OSError as exc:
            raise SessionReplaceError(self._path, exc) from exc

    def list_current_scope(self) -> list[SessionEntry]:
        scope_dir = self._scope_dir()
        if not scope_dir.is_dir():
            return []
        try:
            paths = list(scope_dir.iterdir())
        except OSError as exc:
            raise SessionListError(scope_dir, exc) from exc

        entries: list[SessionEntry] = []
        for path in paths:
            try:
                metadata = path.stat()
            except OSError as exc:
                raise SessionMetadataError(path, exc) from exc
            if not stat.S_ISREG(metadata.st_mode) or path.suffix != ".json":
                continue
            session = self._load_path(path)
            entries.append(
                SessionEntry(
                    name=path.stem,
                    path=path,
                    turns=len(session.turns),
                    active_messages=len(session.active_thread.messages),
                    summarized_turns=session.context.summarized_turns,
                    has_summary=session.context.summary is not None,
                )
            )
        entries.sort(key=lambda entry: entry.name)
        return entries

    def delete(self) -> None:
        removed_primary = _remove_if_exists(self._path)
        removed_legacy = _remove_if_exists(self._legacy_path)
        if not removed_primary and not removed_legacy:
            raise SessionNotFound(self._session_name)

    def rename(self, target_name: str) -> SessionStore:
        target = self._store_for_name(target_name)
        if target.path.is_file():
            raise TargetExists(target.path)
        if target.legacy_path.is_file():
            raise TargetExists(target.legacy_path)

        session = self.load_existing()
        target.save(session)
        _remove_if_exists(self._path)
        _remove_if_exists(self._legacy_path)
        return target

    def export_document_bytes(self) -> bytes:
        return _serialize_document(SessionDocument.new(self.load_existing()), self._path)

    def _load_path(self, path: Path) -> Session:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SessionReadError(path, exc) from exc
        return parse_session_document(path, content)

    def _temp_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.tmp-{os.getpid()}")

    def _scope_dir(self) -> Path:
        return self._root / self._scope

    def _store_for_name(self, session_name: str) -> SessionStore:
        validate_session_name(session_name)
        target = object.__new__(SessionStore)
        target._root = self._root
        target._legacy_root = self._legacy_root
        target._scope = self._scope
        target._session_name = session_name
        file_name = f"{session_name}.json"
        target._path = self._root / self._scope / file_name
        target._legacy_path = self._legacy_root / self._scope / file_name
        return target


def parse_session_document(path: str | Path, content: str) -> Session:
    document_path = Path(path)
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SessionParseError(document_path, str(exc)) from exc
    if not isinstance(value, dict):
        raise SessionParseError(document_path, "session document must be a JSON object")
    version = value.get("schema_version")
    if type(version) is not int or version < 0:  # bool is not a valid JSON u64
        raise SessionParseError(document_path, "missing schema_version")

    try:
        if version == SESSION_DOCUMENT_SCHEMA_VERSION:
            _validate_v3_document_fields(document_path, value)
            return SessionDocument.model_validate(value).session
        if version in (1, THREAD_DOCUMENT_SCHEMA_VERSION):
            _validate_thread_document_fields(document_path, value)
            return Session.from_thread(ThreadDocument.model_validate(value).thread)
    except ValidationError as exc:
        raise SessionParseError(document_path, _validation_detail(exc)) from exc
    raise UnsupportedSchemaVersion(
        document_path,
        version,
        SESSION_DOCUMENT_SCHEMA_VERSION,
    )


def _validate_thread_document_fields(path: Path, value: dict[str, object]) -> None:
    thread = _required_object(path, value, "thread")
    _require_fields(path, thread, "thread", ("messages",))


def _validate_v3_document_fields(path: Path, value: dict[str, object]) -> None:
    session = _required_object(path, value, "session")
    _require_fields(path, session, "session", ("active_thread", "turns", "context"))
    active_thread = _required_object(path, session, "active_thread", prefix="session")
    _require_fields(path, active_thread, "session.active_thread", ("messages",))
    context = _required_object(path, session, "context", prefix="session")
    _require_fields(path, context, "session.context", ("summarized_turns",))


def _required_object(
    path: Path,
    value: dict[str, object],
    field: str,
    *,
    prefix: str | None = None,
) -> dict[str, object]:
    location = f"{prefix}.{field}" if prefix else field
    if field not in value:
        raise SessionParseError(path, f"missing field {location}")
    nested = value[field]
    if not isinstance(nested, dict):
        raise SessionParseError(path, f"field {location} must be a JSON object")
    return nested


def _require_fields(
    path: Path,
    value: dict[str, object],
    prefix: str,
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field not in value:
            raise SessionParseError(path, f"missing field {prefix}.{field}")


def validate_session_name(name: str) -> None:
    if (
        not name
        or not name.isascii()
        or any(not (character.isalnum() or character in "-_") for character in name)
    ):
        raise InvalidSessionName(name)


def _serialize_document(document: SessionDocument, path: Path) -> bytes:
    try:
        value = document.to_wire()
        return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SessionSerializeError(path, exc) from exc


def _remove_if_exists(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SessionRemoveError(path, exc) from exc
    return True


def _validation_detail(error: ValidationError) -> str:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if not details:
        return "invalid session document"
    first = details[0]
    location = ".".join(str(part) for part in first["loc"])
    message = str(first["msg"])
    return f"{location}: {message}" if location else message


__all__ = [
    "CanonicalizeCwdError",
    "CurrentDirError",
    "HomeDirNotFound",
    "InvalidSessionName",
    "SessionCreateDirError",
    "SessionEntry",
    "SessionListError",
    "SessionMetadataError",
    "SessionNotFound",
    "SessionParseError",
    "SessionReadError",
    "SessionRemoveError",
    "SessionReplaceError",
    "SessionSerializeError",
    "SessionStore",
    "SessionStoreError",
    "SessionWriteError",
    "TargetExists",
    "UnsupportedSchemaVersion",
    "parse_session_document",
    "validate_session_name",
]

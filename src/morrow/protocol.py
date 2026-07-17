"""Shared wire protocol and in-memory conversation state for Morrow.

The models in this module intentionally follow the Rust v0.2.0 serde shapes.  In
particular, optional fields that Rust marks with ``skip_serializing_if`` are
omitted while nullable protocol fields such as ``Message.content`` and
``Turn.error`` remain present.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, cast, overload

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


class ProtocolModel(BaseModel):
    """Base class with serde-compatible aliases and JSON serialization."""

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        serialize_by_alias=True,
        validate_assignment=True,
    )

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-compatible representation used on disk and the wire."""

        return self.model_dump(mode="json")


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolDefinitionKind(StrEnum):
    FUNCTION = "function"


class ToolCallKind(StrEnum):
    FUNCTION = "function"


class ToolFunctionDefinition(ProtocolModel):
    name: str
    description: str
    parameters: Any


class _ToolDefinitionFunction:
    @overload
    def __get__(
        self, instance: None, owner: type[ToolDefinition]
    ) -> Callable[[str, str, Any], ToolDefinition]: ...

    @overload
    def __get__(
        self,
        instance: ToolDefinition,
        owner: type[ToolDefinition] | None = None,
    ) -> ToolFunctionDefinition: ...

    def __get__(
        self,
        instance: ToolDefinition | None,
        owner: type[ToolDefinition] | None = None,
    ) -> Callable[[str, str, Any], ToolDefinition] | ToolFunctionDefinition:
        if instance is not None:
            return instance.function_data
        if owner is None:  # pragma: no cover - descriptor protocol always supplies owner
            raise AttributeError("function")

        def construct(name: str, description: str, parameters: Any) -> ToolDefinition:
            return owner(
                kind=ToolDefinitionKind.FUNCTION,
                function_data=ToolFunctionDefinition(
                    name=name,
                    description=description,
                    parameters=parameters,
                ),
            )

        return construct


class ToolDefinition(ProtocolModel):
    kind: ToolDefinitionKind = Field(alias="type")
    function_data: ToolFunctionDefinition = Field(alias="function")
    function: ClassVar[_ToolDefinitionFunction] = _ToolDefinitionFunction()


class ToolFunctionCall(ProtocolModel):
    name: str
    arguments: str


class _ToolCallFunction:
    @overload
    def __get__(
        self, instance: None, owner: type[ToolCall]
    ) -> Callable[[str, str, str], ToolCall]: ...

    @overload
    def __get__(
        self,
        instance: ToolCall,
        owner: type[ToolCall] | None = None,
    ) -> ToolFunctionCall: ...

    def __get__(
        self,
        instance: ToolCall | None,
        owner: type[ToolCall] | None = None,
    ) -> Callable[[str, str, str], ToolCall] | ToolFunctionCall:
        if instance is not None:
            return instance.function_data
        if owner is None:  # pragma: no cover - descriptor protocol always supplies owner
            raise AttributeError("function")

        def construct(id: str, name: str, arguments: str) -> ToolCall:
            return owner(
                id=id,
                kind=ToolCallKind.FUNCTION,
                function_data=ToolFunctionCall(name=name, arguments=arguments),
            )

        return construct


class ToolCall(ProtocolModel):
    id: str
    kind: ToolCallKind = Field(alias="type")
    function_data: ToolFunctionCall = Field(alias="function")
    function: ClassVar[_ToolCallFunction] = _ToolCallFunction()


class Message(ProtocolModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.tool_calls is None:
            data.pop("tool_calls", None)
        if self.tool_call_id is None:
            data.pop("tool_call_id", None)
        return data

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role=Role.ASSISTANT, content=content)

    @classmethod
    def assistant_tool_calls(cls, tool_calls: list[ToolCall]) -> Message:
        return cls(role=Role.ASSISTANT, content=None, tool_calls=tool_calls)

    @classmethod
    def assistant_tool_calls_with_content(cls, content: str, tool_calls: list[ToolCall]) -> Message:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id)


class Conversation(ProtocolModel):
    messages: list[Message] = Field(default_factory=list)

    @classmethod
    def new(cls) -> Conversation:
        return cls()

    @classmethod
    def with_system_prompt(cls, system_prompt: str) -> Conversation:
        conversation = cls.new()
        conversation.push(Message.system(system_prompt))
        return conversation

    def push(self, message: Message) -> None:
        self.messages.append(message)


class Thread(ProtocolModel):
    messages: list[Message] = Field(default_factory=list)

    @classmethod
    def new(cls) -> Thread:
        return cls()

    def push(self, message: Message) -> None:
        self.messages.append(message)


THREAD_DOCUMENT_SCHEMA_VERSION = 2
SESSION_DOCUMENT_SCHEMA_VERSION = 3


class ThreadDocument(ProtocolModel):
    schema_version: int
    thread: Thread

    @classmethod
    def new(cls, thread: Thread) -> ThreadDocument:
        return cls(schema_version=THREAD_DOCUMENT_SCHEMA_VERSION, thread=thread)


class SessionContext(ProtocolModel):
    summary: str | None = None
    summarized_turns: int = Field(default=0, ge=0)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.summary is None:
            data.pop("summary", None)
        return data

    @classmethod
    def new(cls) -> SessionContext:
        return cls()


class PermissionMode(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL_ACCESS = "danger_full_access"

    def as_str(self) -> str:
        return self.value


class ShellPolicy(StrEnum):
    DENY = "deny"
    PROMPT = "prompt"
    ALLOW = "allow"

    def as_str(self) -> str:
        return self.value


class PermissionProfile(ProtocolModel):
    mode: PermissionMode = PermissionMode.READ_ONLY
    shell: ShellPolicy = ShellPolicy.PROMPT

    @model_validator(mode="before")
    @classmethod
    def _default_shell_for_mode(cls, value: Any) -> Any:
        if isinstance(value, dict) and "shell" not in value:
            mode = value.get("mode", PermissionMode.READ_ONLY)
            try:
                parsed_mode = PermissionMode(mode)
            except (TypeError, ValueError):
                return value
            value = dict(value)
            value["shell"] = (
                ShellPolicy.ALLOW
                if parsed_mode is PermissionMode.DANGER_FULL_ACCESS
                else ShellPolicy.PROMPT
            )
        return value

    @classmethod
    def for_mode(cls, mode: PermissionMode) -> PermissionProfile:
        shell = (
            ShellPolicy.ALLOW if mode is PermissionMode.DANGER_FULL_ACCESS else ShellPolicy.PROMPT
        )
        return cls(mode=mode, shell=shell)


class FileChangeOperation(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"

    def as_str(self) -> str:
        return self.value


class FileChangeSummary(ProtocolModel):
    path: str
    operation: FileChangeOperation
    replacements: int = Field(ge=0)
    created: bool
    overwritten: bool
    deleted: bool


class ShellCommandSummary(ProtocolModel):
    command: str
    exit_code: int | None
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


class ApprovalActionKind(StrEnum):
    SHELL_COMMAND = "shell_command"
    FILE_CHANGES = "file_changes"


class ApprovalAction(ProtocolModel):
    """A serde-compatible tagged approval action."""

    kind: ApprovalActionKind
    command: str | None = None
    cwd: Path | None = None
    timeout_secs: int | None = Field(default=None, ge=0)
    files: list[FileChangeSummary] | None = None
    diff: str | None = None

    @model_validator(mode="after")
    def _validate_variant(self) -> ApprovalAction:
        if self.kind is ApprovalActionKind.SHELL_COMMAND:
            if self.command is None or self.cwd is None or self.timeout_secs is None:
                raise ValueError("shell_command requires command, cwd and timeout_secs")
        elif self.files is None or self.diff is None:
            raise ValueError("file_changes requires files and diff")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.kind is ApprovalActionKind.SHELL_COMMAND:
            for field in ("files", "diff"):
                data.pop(field, None)
        else:
            for field in ("command", "cwd", "timeout_secs"):
                data.pop(field, None)
        return data

    @classmethod
    def shell_command(cls, command: str, cwd: str | Path, timeout_secs: int) -> ApprovalAction:
        return cls(
            kind=ApprovalActionKind.SHELL_COMMAND,
            command=command,
            cwd=Path(cwd),
            timeout_secs=timeout_secs,
        )

    @classmethod
    def file_changes(cls, files: list[FileChangeSummary], diff: str) -> ApprovalAction:
        return cls(kind=ApprovalActionKind.FILE_CHANGES, files=files, diff=diff)


class ApprovalRequest(ProtocolModel):
    id: str
    action: ApprovalAction
    reason: str

    @classmethod
    def shell_command(
        cls,
        id: str,
        command: str,
        cwd: str | Path,
        timeout_secs: int,
        reason: str,
    ) -> ApprovalRequest:
        return cls(
            id=id,
            action=ApprovalAction.shell_command(command, cwd, timeout_secs),
            reason=reason,
        )

    @classmethod
    def file_changes(
        cls,
        id: str,
        files: list[FileChangeSummary],
        diff: str,
        reason: str,
    ) -> ApprovalRequest:
        return cls(
            id=id,
            action=ApprovalAction.file_changes(files, diff),
            reason=reason,
        )


class ApprovalDecision(ProtocolModel):
    request_id: str
    approved: bool

    @classmethod
    def approve(cls, request_id: str) -> ApprovalDecision:
        return cls(request_id=request_id, approved=True)

    @classmethod
    def deny(cls, request_id: str) -> ApprovalDecision:
        return cls(request_id=request_id, approved=False)


_SummaryValue = TypeVar("_SummaryValue")


class _SummaryValueFactory(Generic[_SummaryValue]):
    """Expose a Rust-style constructor on the class and value on instances."""

    def __init__(self, field_name: str) -> None:
        self._field_name = field_name

    @overload
    def __get__(
        self, instance: None, owner: type[ToolExecutionSummary]
    ) -> Callable[[_SummaryValue], ToolExecutionSummary]: ...

    @overload
    def __get__(
        self,
        instance: ToolExecutionSummary,
        owner: type[ToolExecutionSummary] | None = None,
    ) -> _SummaryValue | None: ...

    def __get__(
        self,
        instance: ToolExecutionSummary | None,
        owner: type[ToolExecutionSummary] | None = None,
    ) -> Callable[[_SummaryValue], ToolExecutionSummary] | _SummaryValue | None:
        if instance is None:
            if owner is None:  # pragma: no cover - descriptor protocol always supplies owner
                raise AttributeError(self._field_name)

            def construct(value: _SummaryValue) -> ToolExecutionSummary:
                return owner(**{self._field_name: value})

            return construct
        return cast(_SummaryValue | None, getattr(instance, self._field_name))


class ToolExecutionSummary(ProtocolModel):
    files: list[FileChangeSummary] = Field(default_factory=list)
    diff: str | None = None
    shell_summary: ShellCommandSummary | None = Field(default=None, alias="shell")
    error_message: str | None = Field(default=None, alias="error")

    shell: ClassVar[_SummaryValueFactory[ShellCommandSummary]] = _SummaryValueFactory(
        "shell_summary"
    )
    error: ClassVar[_SummaryValueFactory[str]] = _SummaryValueFactory("error_message")

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if not self.files:
            data.pop("files", None)
        if self.diff is None:
            data.pop("diff", None)
        if self.shell_summary is None:
            data.pop("shell", None)
        if self.error_message is None:
            data.pop("error", None)
        return data

    @classmethod
    def file_changes(cls, files: list[FileChangeSummary], diff: str) -> ToolExecutionSummary:
        return cls(files=files, diff=diff)


class TurnStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TurnStepKind(StrEnum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"


class TurnStep(ProtocolModel):
    kind: TurnStepKind
    status: TurnStatus
    tool_name: str | None = None
    tool_call_id: str | None = None
    error: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.tool_name is None:
            data.pop("tool_name", None)
        if self.tool_call_id is None:
            data.pop("tool_call_id", None)
        return data

    @classmethod
    def running(cls, kind: TurnStepKind) -> TurnStep:
        return cls(kind=kind, status=TurnStatus.RUNNING)

    @classmethod
    def running_model_call(cls) -> TurnStep:
        return cls.running(TurnStepKind.MODEL_CALL)

    @classmethod
    def running_tool_call(cls, name: str, id: str) -> TurnStep:
        return cls(
            kind=TurnStepKind.TOOL_CALL,
            status=TurnStatus.RUNNING,
            tool_name=name,
            tool_call_id=id,
        )

    def complete(self) -> None:
        self.status = TurnStatus.COMPLETED
        self.error = None

    def fail(self, error: str) -> None:
        self.status = TurnStatus.FAILED
        self.error = error


class Turn(ProtocolModel):
    status: TurnStatus
    user_message: Message
    assistant_message: Message | None = None
    steps: list[TurnStep]
    error: str | None = None

    @classmethod
    def running(cls, user_message: Message) -> Turn:
        return cls(
            status=TurnStatus.RUNNING,
            user_message=user_message,
            steps=[TurnStep.running_model_call()],
        )

    def complete(self, assistant_message: Message) -> None:
        self.status = TurnStatus.COMPLETED
        self.assistant_message = assistant_message
        self.error = None
        if self.steps:
            self.steps[-1].complete()

    def fail(self, error: str) -> None:
        self.status = TurnStatus.FAILED
        self.error = error
        for step in self.steps:
            if step.status is TurnStatus.RUNNING:
                step.fail(error)


class TurnRecord(ProtocolModel):
    turn: Turn
    messages: list[Message]

    @classmethod
    def new(cls, turn: Turn, messages: list[Message]) -> TurnRecord:
        return cls(turn=turn, messages=messages)

    @classmethod
    def failed_user_prompt(cls, prompt: str, error: str) -> TurnRecord:
        user_message = Message.user(prompt)
        turn = Turn.running(user_message.model_copy(deep=True))
        turn.fail(error)
        return cls(turn=turn, messages=[user_message])


class SessionApplyError(ValueError):
    def __init__(self) -> None:
        super().__init__("cannot apply a running turn to a session")


class Session(ProtocolModel):
    active_thread: Thread = Field(default_factory=Thread.new)
    turns: list[TurnRecord] = Field(default_factory=list)
    context: SessionContext = Field(default_factory=SessionContext.new)

    @classmethod
    def new(cls) -> Session:
        return cls()

    @classmethod
    def from_thread(cls, active_thread: Thread) -> Session:
        return cls(active_thread=active_thread.model_copy(deep=True))

    def try_apply_turn(self, record: TurnRecord) -> None:
        if record.turn.status is TurnStatus.RUNNING:
            raise SessionApplyError
        if record.turn.status is TurnStatus.COMPLETED:
            self.active_thread.messages.extend(
                message.model_copy(deep=True) for message in record.messages
            )
        self.turns.append(record.model_copy(deep=True))

    def apply_turn(self, record: TurnRecord) -> None:
        self.try_apply_turn(record)


class SessionDocument(ProtocolModel):
    schema_version: int
    session: Session

    @classmethod
    def new(cls, session: Session) -> SessionDocument:
        return cls(schema_version=SESSION_DOCUMENT_SCHEMA_VERSION, session=session)


class AgentEventType(StrEnum):
    TURN_STARTED = "turn_started"
    WARNING = "warning"
    TEXT_DELTA = "text_delta"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TURN_COMPLETED = "turn_completed"
    ERROR = "error"


class ToolCallStartedData(ProtocolModel):
    id: str
    name: str


class ToolCallFinishedData(ProtocolModel):
    id: str
    name: str
    ok: bool
    summary: ToolExecutionSummary | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.summary is None:
            data.pop("summary", None)
        return data


_STRUCTURED_EVENT_DATA: dict[AgentEventType, type[ProtocolModel]] = {
    AgentEventType.TOOL_CALL_STARTED: ToolCallStartedData,
    AgentEventType.TOOL_CALL_FINISHED: ToolCallFinishedData,
    AgentEventType.APPROVAL_REQUESTED: ApprovalRequest,
    AgentEventType.APPROVAL_RESOLVED: ApprovalDecision,
}
_STRING_EVENT_TYPES = {
    AgentEventType.WARNING,
    AgentEventType.TEXT_DELTA,
    AgentEventType.AGENT_MESSAGE,
    AgentEventType.ERROR,
}
_UNIT_EVENT_TYPES = {AgentEventType.TURN_STARTED, AgentEventType.TURN_COMPLETED}


class AgentEvent(ProtocolModel):
    type: AgentEventType
    data: Any = None

    @model_validator(mode="before")
    @classmethod
    def _parse_variant_data(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "type" not in value:
            return value
        try:
            event_type = AgentEventType(value["type"])
        except (TypeError, ValueError):
            return value
        parsed = dict(value)
        if event_type in _UNIT_EVENT_TYPES:
            parsed["data"] = None
        elif event_type in _STRING_EVENT_TYPES:
            data = parsed.get("data")
            if not isinstance(data, str):
                raise ValueError(f"{event_type.value} requires string data")
        else:
            model = _STRUCTURED_EVENT_DATA[event_type]
            parsed["data"] = model.model_validate(parsed.get("data"))
        return parsed

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.data is None:
            data.pop("data", None)
        return data

    @classmethod
    def turn_started(cls) -> AgentEvent:
        return cls(type=AgentEventType.TURN_STARTED)

    @classmethod
    def warning(cls, message: str) -> AgentEvent:
        return cls(type=AgentEventType.WARNING, data=message)

    @classmethod
    def text_delta(cls, text: str) -> AgentEvent:
        return cls(type=AgentEventType.TEXT_DELTA, data=text)

    @classmethod
    def agent_message(cls, message: str) -> AgentEvent:
        return cls(type=AgentEventType.AGENT_MESSAGE, data=message)

    @classmethod
    def tool_call_started(cls, id: str, name: str) -> AgentEvent:
        return cls(
            type=AgentEventType.TOOL_CALL_STARTED,
            data=ToolCallStartedData(id=id, name=name),
        )

    @classmethod
    def tool_call_finished(
        cls,
        id: str,
        name: str,
        ok: bool,
        summary: ToolExecutionSummary | None = None,
    ) -> AgentEvent:
        return cls(
            type=AgentEventType.TOOL_CALL_FINISHED,
            data=ToolCallFinishedData(id=id, name=name, ok=ok, summary=summary),
        )

    @classmethod
    def approval_requested(cls, request: ApprovalRequest) -> AgentEvent:
        return cls(type=AgentEventType.APPROVAL_REQUESTED, data=request)

    @classmethod
    def approval_resolved(cls, decision: ApprovalDecision) -> AgentEvent:
        return cls(type=AgentEventType.APPROVAL_RESOLVED, data=decision)

    @classmethod
    def turn_completed(cls) -> AgentEvent:
        return cls(type=AgentEventType.TURN_COMPLETED)

    @classmethod
    def error(cls, message: str) -> AgentEvent:
        return cls(type=AgentEventType.ERROR, data=message)


__all__ = [
    "SESSION_DOCUMENT_SCHEMA_VERSION",
    "THREAD_DOCUMENT_SCHEMA_VERSION",
    "AgentEvent",
    "AgentEventType",
    "ApprovalAction",
    "ApprovalActionKind",
    "ApprovalDecision",
    "ApprovalRequest",
    "Conversation",
    "FileChangeOperation",
    "FileChangeSummary",
    "Message",
    "PermissionMode",
    "PermissionProfile",
    "ProtocolModel",
    "Role",
    "Session",
    "SessionApplyError",
    "SessionContext",
    "SessionDocument",
    "ShellCommandSummary",
    "ShellPolicy",
    "Thread",
    "ThreadDocument",
    "ToolCall",
    "ToolCallFinishedData",
    "ToolCallKind",
    "ToolCallStartedData",
    "ToolDefinition",
    "ToolDefinitionKind",
    "ToolExecutionSummary",
    "ToolFunctionCall",
    "ToolFunctionDefinition",
    "Turn",
    "TurnRecord",
    "TurnStatus",
    "TurnStep",
    "TurnStepKind",
]

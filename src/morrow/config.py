"""Strict TOML configuration loading for Morrow.

Configuration precedence and validation mirror the Rust v0.2.0 implementation:
an explicit file wins, followed by ``./morrow.toml`` and then
``~/.morrow/config.toml``.
"""

from __future__ import annotations

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from morrow.protocol import PermissionMode, PermissionProfile, ShellPolicy

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_TIMEOUT_SECS = 120
DEFAULT_RESERVED_OUTPUT_TOKENS = 8_192
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
DEFAULT_AUTO_COMPACT = True
DEFAULT_AUTO_COMPACT_THRESHOLD = 0.835
DEFAULT_RETAIN_RECENT_TURNS = 6
DEFAULT_SUMMARY_TARGET_TOKENS = 12_000
DEFAULT_COMPACT_MAX_RETRIES = 2
DEFAULT_MCP_STARTUP_TIMEOUT_SECS = 10
DEFAULT_MCP_TOOL_TIMEOUT_SECS = 60
DEFAULT_PLC_SUBAGENT_TIMEOUT_SECS = 600


class ConfigError(Exception):
    """Base class for user-facing configuration errors."""


class ExplicitConfigNotFound(ConfigError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"config file not found: {path}")


class NoConfigFile(ConfigError):
    def __init__(self, searched: list[Path]) -> None:
        self.searched = searched
        joined = ", ".join(str(path) for path in searched)
        super().__init__(f"no config file found; searched: {joined}")


class ConfigReadError(ConfigError):
    def __init__(self, path: Path, source: BaseException) -> None:
        self.path = path
        self.source = source
        super().__init__(f"failed to read config file {path}: {source}")


class ConfigParseError(ConfigError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"failed to parse config file {path}: {detail}")


class MissingModel(ConfigError):
    def __init__(self) -> None:
        super().__init__("missing required config value: [model].model")


class MissingContextWindowTokens(ConfigError):
    def __init__(self) -> None:
        super().__init__("missing required config value: [model].context_window_tokens")


class MissingApiKey(ConfigError):
    def __init__(self, env_var: str) -> None:
        self.env_var = env_var
        super().__init__(f"configured API key environment variable {env_var} is not set")


class InvalidPositiveValue(ConfigError):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"invalid config value: {field} must be greater than 0")


class InvalidAutoCompactThreshold(ConfigError):
    def __init__(self) -> None:
        super().__init__(
            "invalid config value: [context].auto_compact_threshold must be greater "
            "than 0 and less than or equal to 1"
        )


class InvalidMcpPositiveValue(ConfigError):
    def __init__(self, server: str, field: str) -> None:
        self.server = server
        self.field = field
        super().__init__(
            f"invalid config value: [mcp_servers.{server}].{field} must be greater than 0"
        )


class MissingMcpCommand(ConfigError):
    def __init__(self, server: str) -> None:
        self.server = server
        super().__init__(f"missing required config value: [mcp_servers.{server}].command")


class MissingMcpUrl(ConfigError):
    def __init__(self, server: str) -> None:
        self.server = server
        super().__init__(f"missing required config value: [mcp_servers.{server}].url")


class MissingMcpEnvVar(ConfigError):
    def __init__(self, server: str, field: str, env_var: str) -> None:
        self.server = server
        self.field = field
        self.env_var = env_var
        super().__init__(
            f"configured MCP environment variable {env_var} for "
            f"[mcp_servers.{server}].{field} is not set"
        )


class UnsupportedMcpField(ConfigError):
    def __init__(self, server: str, field: str) -> None:
        self.server = server
        self.field = field
        super().__init__(f"unsupported MCP config value: [mcp_servers.{server}].{field}")


class MissingPlcSubagentBaseUrl(ConfigError):
    def __init__(self) -> None:
        super().__init__("missing required config value: [plc_subagents].base_url when enabled")


class InvalidPlcSubagentBaseUrl(ConfigError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"invalid config value: [plc_subagents].base_url {detail}")


@dataclass(slots=True)
class ModelContextLimits:
    context_window_tokens: int
    reserved_output_tokens: int


@dataclass(slots=True, repr=False)
class ModelConfig:
    base_url: str
    model: str
    api_key_env: str
    timeout_secs: int
    context_window_tokens: int
    reserved_output_tokens: int

    def context_limits(self) -> ModelContextLimits:
        return ModelContextLimits(
            context_window_tokens=self.context_window_tokens,
            reserved_output_tokens=self.reserved_output_tokens,
        )

    def __repr__(self) -> str:
        return (
            "ModelConfig(base_url='<configured>', "
            f"model={self.model!r}, api_key_env={self.api_key_env!r}, "
            f"timeout_secs={self.timeout_secs!r}, "
            f"context_window_tokens={self.context_window_tokens!r}, "
            f"reserved_output_tokens={self.reserved_output_tokens!r})"
        )


@dataclass(slots=True)
class AgentConfig:
    system_prompt: str


@dataclass(slots=True)
class ContextConfig:
    auto_compact: bool
    auto_compact_threshold: float
    retain_recent_turns: int
    summary_target_tokens: int
    compact_max_retries: int


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


@dataclass(slots=True, repr=False)
class McpServerConfig:
    name: str
    transport: McpTransport
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: Path | None
    url: str | None
    http_headers: dict[str, str]
    enabled: bool
    startup_timeout_sec: int
    tool_timeout_sec: int

    def __repr__(self) -> str:
        env_keys = sorted(self.env)
        header_keys = sorted(self.http_headers)
        configured_url = "<configured>" if self.url is not None else None
        return (
            f"McpServerConfig(name={self.name!r}, transport={self.transport!r}, "
            f"command={self.command!r}, args='<{len(self.args)} entries>', "
            f"env={env_keys!r}, cwd={self.cwd!r}, url={configured_url!r}, "
            f"http_headers={header_keys!r}, enabled={self.enabled!r}, "
            f"startup_timeout_sec={self.startup_timeout_sec!r}, "
            f"tool_timeout_sec={self.tool_timeout_sec!r})"
        )


@dataclass(slots=True, repr=False)
class PlcSubagentConfig:
    enabled: bool = False
    base_url: str | None = None
    timeout_secs: int = DEFAULT_PLC_SUBAGENT_TIMEOUT_SECS

    def __repr__(self) -> str:
        configured_url = "<configured>" if self.base_url is not None else None
        return (
            f"PlcSubagentConfig(enabled={self.enabled!r}, base_url={configured_url!r}, "
            f"timeout_secs={self.timeout_secs!r})"
        )


@dataclass(slots=True)
class AppConfig:
    model: ModelConfig
    agent: AgentConfig
    context: ContextConfig
    permissions: PermissionProfile
    mcp_servers: list[McpServerConfig]
    plc_subagents: PlcSubagentConfig = field(default_factory=PlcSubagentConfig)


@dataclass(slots=True, repr=False)
class LoadedConfig:
    config: AppConfig
    path: Path
    api_key: str

    def __repr__(self) -> str:
        return f"LoadedConfig(config={self.config!r}, path={self.path!r}, api_key='<redacted>')"


class _RawModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _RawModelConfig(_RawModel):
    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    timeout_secs: int | None = Field(default=None, ge=0)
    context_window_tokens: int | None = Field(default=None, ge=0)
    reserved_output_tokens: int | None = Field(default=None, ge=0)


class _RawAgentConfig(_RawModel):
    system_prompt: str | None = None


class _RawContextConfig(_RawModel):
    auto_compact: bool | None = None
    auto_compact_threshold: float | None = None
    retain_recent_turns: int | None = Field(default=None, ge=0)
    summary_target_tokens: int | None = Field(default=None, ge=0)
    compact_max_retries: int | None = Field(default=None, ge=0)


class _RawPermissionsConfig(_RawModel):
    mode: str | None = None
    shell: str | None = None


class _RawMcpServerConfig(_RawModel):
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool | None = None
    startup_timeout_sec: int | None = Field(default=None, ge=0)
    tool_timeout_sec: int | None = Field(default=None, ge=0)
    url: str | None = None
    bearer_token_env_var: str | None = None
    http_headers: dict[str, str] = Field(default_factory=dict)
    env_http_headers: dict[str, str] = Field(default_factory=dict)
    oauth_client_id: str | None = None
    oauth_resource: str | None = None


class _RawPlcSubagentConfig(_RawModel):
    enabled: bool | None = None
    base_url: str | None = None
    timeout_secs: int | None = Field(default=None, ge=0)


class _RawAppConfig(_RawModel):
    model: _RawModelConfig | None = None
    agent: _RawAgentConfig | None = None
    context: _RawContextConfig | None = None
    permissions: _RawPermissionsConfig | None = None
    mcp_servers: dict[str, _RawMcpServerConfig] = Field(default_factory=dict)
    plc_subagents: _RawPlcSubagentConfig | None = None


class _InvalidRawValue(Exception):
    pass


def load_config(explicit_path: str | Path | None = None) -> LoadedConfig:
    """Load Morrow configuration using the standard location precedence."""

    cwd = Path.cwd()
    try:
        home: Path | None = Path.home()
    except RuntimeError:
        home = None
    return load_config_from_locations(explicit_path, cwd, home)


def load_config_from_locations(
    explicit_path: str | Path | None,
    cwd: str | Path,
    home: str | Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """Load from injected locations; useful for embedding and deterministic tests."""

    path = select_config_path(explicit_path, cwd, home)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigReadError(path, exc) from exc

    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(path, str(exc)) from exc

    try:
        raw = _RawAppConfig.model_validate(parsed)
    except ValidationError as exc:
        raise ConfigParseError(path, _safe_validation_detail(exc)) from exc

    inline_api_key = None
    if raw.model is not None and raw.model.openai_api_key is not None:
        candidate = raw.model.openai_api_key.strip()
        if candidate:
            inline_api_key = candidate

    environment = os.environ if environ is None else environ
    try:
        config = _build_config(raw, environment)
    except _InvalidRawValue as exc:
        raise ConfigParseError(path, str(exc)) from exc

    if inline_api_key is not None:
        api_key = inline_api_key
    else:
        try:
            api_key = environment[config.model.api_key_env]
        except KeyError as exc:
            raise MissingApiKey(config.model.api_key_env) from exc

    return LoadedConfig(config=config, path=path, api_key=api_key)


def select_config_path(
    explicit_path: str | Path | None,
    cwd: str | Path,
    home: str | Path | None,
) -> Path:
    if explicit_path is not None:
        explicit = Path(explicit_path)
        if explicit.is_file():
            return explicit
        raise ExplicitConfigNotFound(explicit)

    local = Path(cwd) / "morrow.toml"
    if local.is_file():
        return local

    searched = [local]
    if home is not None:
        user = Path(home) / ".morrow" / "config.toml"
        if user.is_file():
            return user
        searched.append(user)
    raise NoConfigFile(searched)


def _safe_validation_detail(error: ValidationError) -> str:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if not details:
        return "invalid configuration"
    first = details[0]
    location = ".".join(str(part) for part in first["loc"])
    message = str(first["msg"])
    return f"{location}: {message}" if location else message


def _build_config(raw: _RawAppConfig, environ: Mapping[str, str]) -> AppConfig:
    model = raw.model or _RawModelConfig()
    model_name = model.model.strip() if model.model is not None else ""
    if not model_name:
        raise MissingModel
    if model.context_window_tokens is None:
        raise MissingContextWindowTokens

    context_window_tokens = _positive("[model].context_window_tokens", model.context_window_tokens)
    reserved_output_tokens = _positive(
        "[model].reserved_output_tokens",
        model.reserved_output_tokens
        if model.reserved_output_tokens is not None
        else DEFAULT_RESERVED_OUTPUT_TOKENS,
    )

    context = _build_context(raw.context or _RawContextConfig())
    permissions = _build_permissions(raw.permissions or _RawPermissionsConfig())
    agent = raw.agent or _RawAgentConfig()

    return AppConfig(
        model=ModelConfig(
            base_url=model.base_url if model.base_url is not None else DEFAULT_BASE_URL,
            model=model_name,
            api_key_env=(
                model.api_key_env if model.api_key_env is not None else DEFAULT_API_KEY_ENV
            ),
            timeout_secs=(
                model.timeout_secs if model.timeout_secs is not None else DEFAULT_TIMEOUT_SECS
            ),
            context_window_tokens=context_window_tokens,
            reserved_output_tokens=reserved_output_tokens,
        ),
        agent=AgentConfig(
            system_prompt=(
                agent.system_prompt if agent.system_prompt is not None else DEFAULT_SYSTEM_PROMPT
            )
        ),
        context=context,
        permissions=permissions,
        mcp_servers=_parse_mcp_servers(raw.mcp_servers, environ),
        plc_subagents=_build_plc_subagents(raw.plc_subagents or _RawPlcSubagentConfig()),
    )


def _build_context(raw: _RawContextConfig) -> ContextConfig:
    threshold = (
        raw.auto_compact_threshold
        if raw.auto_compact_threshold is not None
        else DEFAULT_AUTO_COMPACT_THRESHOLD
    )
    if not math.isfinite(threshold) or threshold <= 0.0 or threshold > 1.0:
        raise InvalidAutoCompactThreshold

    return ContextConfig(
        auto_compact=(raw.auto_compact if raw.auto_compact is not None else DEFAULT_AUTO_COMPACT),
        auto_compact_threshold=threshold,
        retain_recent_turns=_positive(
            "[context].retain_recent_turns",
            raw.retain_recent_turns
            if raw.retain_recent_turns is not None
            else DEFAULT_RETAIN_RECENT_TURNS,
        ),
        summary_target_tokens=_positive(
            "[context].summary_target_tokens",
            raw.summary_target_tokens
            if raw.summary_target_tokens is not None
            else DEFAULT_SUMMARY_TARGET_TOKENS,
        ),
        compact_max_retries=_positive(
            "[context].compact_max_retries",
            raw.compact_max_retries
            if raw.compact_max_retries is not None
            else DEFAULT_COMPACT_MAX_RETRIES,
        ),
    )


def _build_permissions(raw: _RawPermissionsConfig) -> PermissionProfile:
    try:
        mode = PermissionMode(raw.mode) if raw.mode is not None else PermissionMode.READ_ONLY
    except ValueError as exc:
        raise _InvalidRawValue(f"permissions.mode: invalid value {raw.mode!r}") from exc
    profile = PermissionProfile.for_mode(mode)
    if raw.shell is not None:
        try:
            profile.shell = ShellPolicy(raw.shell)
        except ValueError as exc:
            raise _InvalidRawValue(f"permissions.shell: invalid value {raw.shell!r}") from exc
    return profile


def _positive(field: str, value: int) -> int:
    if value == 0:
        raise InvalidPositiveValue(field)
    return value


def _build_plc_subagents(raw: _RawPlcSubagentConfig) -> PlcSubagentConfig:
    enabled = raw.enabled if raw.enabled is not None else False
    timeout_secs = _positive(
        "[plc_subagents].timeout_secs",
        raw.timeout_secs if raw.timeout_secs is not None else DEFAULT_PLC_SUBAGENT_TIMEOUT_SECS,
    )
    candidate = raw.base_url.strip() if raw.base_url is not None else ""
    if not candidate:
        if enabled:
            raise MissingPlcSubagentBaseUrl
        return PlcSubagentConfig(enabled=False, timeout_secs=timeout_secs)
    return PlcSubagentConfig(
        enabled=enabled,
        base_url=_normalize_plc_subagent_base_url(candidate),
        timeout_secs=timeout_secs,
    )


def _normalize_plc_subagent_base_url(value: str) -> str:
    if any(character.isspace() for character in value):
        raise InvalidPlcSubagentBaseUrl("must not contain whitespace")
    if "?" in value or "#" in value:
        raise InvalidPlcSubagentBaseUrl("must not contain a query or fragment")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InvalidPlcSubagentBaseUrl("must be a valid URL") from exc
    del port
    if parsed.scheme not in {"http", "https"}:
        raise InvalidPlcSubagentBaseUrl("must use http or https")
    if parsed.hostname is None:
        raise InvalidPlcSubagentBaseUrl("must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidPlcSubagentBaseUrl("must not contain authentication information")
    return value.rstrip("/")


def _parse_mcp_servers(
    raw_servers: Mapping[str, _RawMcpServerConfig], environ: Mapping[str, str]
) -> list[McpServerConfig]:
    servers: list[McpServerConfig] = []
    for name in sorted(raw_servers):
        raw = raw_servers[name]
        if raw.oauth_client_id is not None:
            raise UnsupportedMcpField(name, "oauth_client_id")
        if raw.oauth_resource is not None:
            raise UnsupportedMcpField(name, "oauth_resource")

        startup_timeout_sec = (
            raw.startup_timeout_sec
            if raw.startup_timeout_sec is not None
            else DEFAULT_MCP_STARTUP_TIMEOUT_SECS
        )
        if startup_timeout_sec == 0:
            raise InvalidMcpPositiveValue(name, "startup_timeout_sec")
        tool_timeout_sec = (
            raw.tool_timeout_sec
            if raw.tool_timeout_sec is not None
            else DEFAULT_MCP_TOOL_TIMEOUT_SECS
        )
        if tool_timeout_sec == 0:
            raise InvalidMcpPositiveValue(name, "tool_timeout_sec")

        enabled = raw.enabled if raw.enabled is not None else True
        if raw.url is None:
            if raw.bearer_token_env_var is not None:
                raise UnsupportedMcpField(name, "bearer_token_env_var")
            if raw.http_headers:
                raise UnsupportedMcpField(name, "http_headers")
            if raw.env_http_headers:
                raise UnsupportedMcpField(name, "env_http_headers")

            command = raw.command.strip() if raw.command is not None else ""
            if not command:
                raise MissingMcpCommand(name)
            servers.append(
                McpServerConfig(
                    name=name,
                    transport=McpTransport.STDIO,
                    command=command,
                    args=list(raw.args),
                    env=dict(sorted(raw.env.items())),
                    cwd=Path(raw.cwd) if raw.cwd is not None else None,
                    url=None,
                    http_headers={},
                    enabled=enabled,
                    startup_timeout_sec=startup_timeout_sec,
                    tool_timeout_sec=tool_timeout_sec,
                )
            )
            continue

        url = raw.url.strip()
        if not url:
            raise MissingMcpUrl(name)
        http_headers = dict(sorted(raw.http_headers.items()))
        for header, env_var in sorted(raw.env_http_headers.items()):
            try:
                value = environ[env_var]
            except KeyError as exc:
                raise MissingMcpEnvVar(name, f"env_http_headers.{header}", env_var) from exc
            http_headers[header] = value
        if raw.bearer_token_env_var is not None:
            env_var = raw.bearer_token_env_var
            token = environ.get(env_var, "").strip()
            if not token:
                raise MissingMcpEnvVar(name, "bearer_token_env_var", env_var)
            http_headers["Authorization"] = f"Bearer {token}"

        servers.append(
            McpServerConfig(
                name=name,
                transport=McpTransport.HTTP,
                command="",
                args=[],
                env=dict(sorted(raw.env.items())),
                cwd=None,
                url=url,
                http_headers=dict(sorted(http_headers.items())),
                enabled=enabled,
                startup_timeout_sec=startup_timeout_sec,
                tool_timeout_sec=tool_timeout_sec,
            )
        )
    return servers


__all__ = [
    "AgentConfig",
    "AppConfig",
    "ConfigError",
    "ConfigParseError",
    "ConfigReadError",
    "ContextConfig",
    "DEFAULT_PLC_SUBAGENT_TIMEOUT_SECS",
    "ExplicitConfigNotFound",
    "InvalidAutoCompactThreshold",
    "InvalidMcpPositiveValue",
    "InvalidPlcSubagentBaseUrl",
    "InvalidPositiveValue",
    "LoadedConfig",
    "McpServerConfig",
    "McpTransport",
    "MissingApiKey",
    "MissingContextWindowTokens",
    "MissingMcpCommand",
    "MissingMcpEnvVar",
    "MissingMcpUrl",
    "MissingModel",
    "MissingPlcSubagentBaseUrl",
    "ModelConfig",
    "ModelContextLimits",
    "NoConfigFile",
    "PlcSubagentConfig",
    "UnsupportedMcpField",
    "load_config",
    "load_config_from_locations",
    "select_config_path",
]

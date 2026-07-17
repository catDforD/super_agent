from __future__ import annotations

from pathlib import Path

import pytest

from morrow.config import (
    DEFAULT_AUTO_COMPACT_THRESHOLD,
    DEFAULT_BASE_URL,
    DEFAULT_MCP_STARTUP_TIMEOUT_SECS,
    DEFAULT_MCP_TOOL_TIMEOUT_SECS,
    ConfigParseError,
    InvalidAutoCompactThreshold,
    InvalidMcpPositiveValue,
    McpTransport,
    MissingApiKey,
    MissingContextWindowTokens,
    MissingMcpEnvVar,
    MissingModel,
    UnsupportedMcpField,
    load_config_from_locations,
)
from morrow.protocol import PermissionMode, ShellPolicy


def _write_config(path: Path, model: str, api_key_env: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
[model]
model = "{model}"
api_key_env = "{api_key_env}"
context_window_tokens = 65536
""",
        encoding="utf-8",
    )


def test_explicit_then_local_then_home_precedence(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    explicit = tmp_path / "explicit.toml"
    cwd.mkdir()
    _write_config(cwd / "morrow.toml", "local", "LOCAL_KEY")
    _write_config(home / ".morrow" / "config.toml", "home", "HOME_KEY")
    _write_config(explicit, "explicit", "EXPLICIT_KEY")

    loaded = load_config_from_locations(explicit, cwd, home, environ={"EXPLICIT_KEY": "secret"})
    assert loaded.path == explicit
    assert loaded.config.model.model == "explicit"

    loaded = load_config_from_locations(None, cwd, home, environ={"LOCAL_KEY": "secret"})
    assert loaded.path == cwd / "morrow.toml"
    assert loaded.config.model.model == "local"


def test_defaults_inline_key_context_and_permissions(tmp_path: Path) -> None:
    config = tmp_path / "morrow.toml"
    config.write_text(
        """
[model]
model = " test-model "
OPENAI_API_KEY = " inline-secret "
context_window_tokens = 131072

[permissions]
mode = "workspace_write"
shell = "deny"
""",
        encoding="utf-8",
    )

    loaded = load_config_from_locations(config, tmp_path, None, environ={})

    assert loaded.api_key == "inline-secret"
    assert loaded.config.model.model == "test-model"
    assert loaded.config.model.base_url == DEFAULT_BASE_URL
    assert loaded.config.context.auto_compact_threshold == DEFAULT_AUTO_COMPACT_THRESHOLD
    assert loaded.config.permissions.mode is PermissionMode.WORKSPACE_WRITE
    assert loaded.config.permissions.shell is ShellPolicy.DENY


def test_missing_required_model_values_and_api_key(tmp_path: Path) -> None:
    config = tmp_path / "morrow.toml"
    config.write_text("[model]\ncontext_window_tokens=1\n", encoding="utf-8")
    with pytest.raises(MissingModel):
        load_config_from_locations(config, tmp_path, None, environ={})

    config.write_text('[model]\nmodel="x"\n', encoding="utf-8")
    with pytest.raises(MissingContextWindowTokens):
        load_config_from_locations(config, tmp_path, None, environ={})

    _write_config(config, "x", "DOES_NOT_EXIST")
    with pytest.raises(MissingApiKey):
        load_config_from_locations(config, tmp_path, None, environ={})


def test_strict_unknown_fields_and_invalid_threshold(tmp_path: Path) -> None:
    config = tmp_path / "morrow.toml"
    config.write_text(
        """
[model]
model = "x"
OPENAI_API_KEY = "secret"
context_window_tokens = 65536

[context]
max_context_chars = 1024
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigParseError):
        load_config_from_locations(config, tmp_path, None, environ={})

    config.write_text(
        """
[model]
model = "x"
OPENAI_API_KEY = "secret"
context_window_tokens = 65536

[context]
auto_compact_threshold = 1.5
""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidAutoCompactThreshold):
        load_config_from_locations(config, tmp_path, None, environ={})


def test_loads_stdio_and_http_mcp_servers_in_sorted_order(tmp_path: Path) -> None:
    config = tmp_path / "morrow.toml"
    config.write_text(
        """
[model]
model = "x"
OPENAI_API_KEY = "secret"
context_window_tokens = 65536

[mcp_servers.z_stdio]
command = " npx "
args = ["-y", "server"]
env = { FOO = "bar" }
cwd = "."

[mcp_servers.a_http]
url = " https://example.com/mcp "
http_headers = { "X-Static" = "yes" }
env_http_headers = { "X-Env" = "MCP_HEADER" }
bearer_token_env_var = "MCP_TOKEN"
""",
        encoding="utf-8",
    )

    loaded = load_config_from_locations(
        config,
        tmp_path,
        None,
        environ={"MCP_HEADER": "from-env", "MCP_TOKEN": " token "},
    )

    http, stdio = loaded.config.mcp_servers
    assert http.name == "a_http"
    assert http.transport is McpTransport.HTTP
    assert http.command == ""
    assert http.http_headers["Authorization"] == "Bearer token"
    assert http.http_headers["X-Env"] == "from-env"
    assert stdio.transport is McpTransport.STDIO
    assert stdio.command == "npx"
    assert stdio.startup_timeout_sec == DEFAULT_MCP_STARTUP_TIMEOUT_SECS
    assert stdio.tool_timeout_sec == DEFAULT_MCP_TOOL_TIMEOUT_SECS


def test_mcp_validation_and_secret_safe_repr(tmp_path: Path) -> None:
    config = tmp_path / "morrow.toml"
    config.write_text(
        """
[model]
base_url = "https://example.com/v1?token=model-url-secret"
model = "x"
OPENAI_API_KEY = "model-secret"
context_window_tokens = 65536

[mcp_servers.remote]
url = "https://example.com/mcp?token=mcp-url-secret"
http_headers = { Authorization = "Bearer mcp-secret" }
""",
        encoding="utf-8",
    )
    loaded = load_config_from_locations(config, tmp_path, None, environ={})
    debug = repr(loaded)
    assert "model-secret" not in debug
    assert "model-url-secret" not in debug
    assert "mcp-secret" not in debug
    assert "mcp-url-secret" not in debug
    assert "<redacted>" in debug

    content = config.read_text(encoding="utf-8").replace(
        'http_headers = { Authorization = "Bearer mcp-secret" }',
        'oauth_client_id = "client"',
    )
    config.write_text(content, encoding="utf-8")
    with pytest.raises(UnsupportedMcpField):
        load_config_from_locations(config, tmp_path, None, environ={})


def test_mcp_timeout_and_missing_environment_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "morrow.toml"
    config.write_text(
        """
[model]
model = "x"
OPENAI_API_KEY = "secret"
context_window_tokens = 65536

[mcp_servers.remote]
url = "https://example.com/mcp"
tool_timeout_sec = 0
""",
        encoding="utf-8",
    )
    with pytest.raises(InvalidMcpPositiveValue):
        load_config_from_locations(config, tmp_path, None, environ={})

    config.write_text(
        config.read_text(encoding="utf-8")
        .replace("tool_timeout_sec = 0", "")
        .replace(
            'url = "https://example.com/mcp"',
            'url = "https://example.com/mcp"\nenv_http_headers = { X = "MISSING" }',
        ),
        encoding="utf-8",
    )
    with pytest.raises(MissingMcpEnvVar):
        load_config_from_locations(config, tmp_path, None, environ={})

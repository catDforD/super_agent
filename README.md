# Morrow Python

Morrow is a local coding-agent CLI and web dashboard backed by an OpenAI-compatible Chat
Completions API. This repository is the native Python implementation and continues to use the
`morrow` command, configuration, Session storage, and browser protocol from the Rust version.

## Features

- One-shot CLI, interactive REPL, JSONL automation, and local React dashboard.
- Streaming model output and multi-round function calling.
- Project-scoped persistent Sessions with automatic context compaction.
- File reads, listings, literal search, edits, writes, transactional patches, and shell commands.
- Read-only, workspace-write, and danger-full-access permission profiles with explicit approvals.
- MCP stdio and Streamable HTTP tools through the official Python MCP SDK.
- Optional remote PLC development, compile diagnostics, formal verification, and testing tools.

## Install and develop

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --dev
uv run morrow init --template
uv run morrow "summarize this repository"
```

Install a GitHub Release wheel as an isolated tool:

```bash
uv tool install --force ./morrow_py-0.1.0-py3-none-any.whl
morrow init
```

## Configure

Morrow searches, in order, an explicit `--config` path, `morrow.toml` in the current directory,
and `~/.morrow/config.toml`. See `morrow.example.toml` for all supported sections.

```toml
[model]
base_url = "https://api.openai.com/v1"
model = "gpt-4.1"
api_key_env = "OPENAI_API_KEY"
context_window_tokens = 128000

[permissions]
mode = "read_only"
shell = "deny"
```

An inline `[model].OPENAI_API_KEY` takes precedence over `api_key_env`; treat files containing it
as private.

### PLC subagents

The four PLC tools are disabled unless explicitly configured:

```toml
[plc_subagents]
enabled = true
base_url = "http://60.188.37.6:28080"
timeout_secs = 600
```

Enabling this section registers `plc_develop`, `plc_repair`, `plc_formal_verify`, and `plc_test`.
Requirements, complete ST source, formal properties, and test inputs supplied to these tools are
sent to the configured remote service. Calls use independent remote sessions and do not write
remote results or artifacts into the local workspace automatically.

`plc_repair` is a compile-diagnostic tool: the upstream service reports errors and a remote file
name but does not expose the repaired source. Morrow therefore returns `fixed_code: null`; the main
agent must propose or apply any local source change separately. See `docs/subagent_api.md` for the
tested remote contract and upstream limitations.

## Run

```bash
morrow "inspect this project"
morrow --session work "continue"
morrow --jsonl "inspect this project" > events.jsonl
morrow server
```

With no prompt, Morrow starts a REPL. Commands include `/status`, `/compact`, `/reset`,
`/permissions <mode>`, and `/exit`.

Session management:

```bash
morrow session list
morrow session show work
morrow session export work --output work.json
morrow session rename work backend
morrow session delete backend
```

The dashboard defaults to `127.0.0.1:3000`. Browser WebSockets are restricted to the dashboard's
origin, but the service is otherwise intentionally local and unauthenticated; do not expose it
publicly without an external security layer.

## Frontend development

```bash
pnpm --dir web install --frozen-lockfile
pnpm --dir web dev
```

The Vite server proxies `/api` to the Python server on port 3000. Build package assets with:

```bash
pnpm --dir web typecheck
pnpm --dir web build
```

## Verification

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv build
```

# Repository Guidelines

## Project layout

- `src/morrow/protocol.py`: shared wire and domain models.
- `src/morrow/core.py`: model/tool ports and the agent turn state machine.
- `src/morrow/model.py`: OpenAI-compatible streaming adapter.
- `src/morrow/tools/` and `src/morrow/sandbox.py`: built-in tools, MCP, and permissions.
- `src/morrow/runtime/`: turn orchestration, compaction, events, and Session storage.
- `src/morrow/cli.py` and `src/morrow/server/`: CLI, HTTP/WebSocket API, and packaged UI.
- `web/`: React/Vite dashboard; build output is packaged under `src/morrow/server/assets/`.

## Development commands

- `uv sync --dev`: install Python and development dependencies.
- `uv run pytest`: run Python tests.
- `uv run ruff format --check .`: check formatting.
- `uv run ruff check .`: run lint checks.
- `uv run mypy src`: run strict type checking.
- `uv run morrow --help`: run the CLI from source.
- `pnpm --dir web install --frozen-lockfile`: install UI dependencies.
- `pnpm --dir web typecheck && pnpm --dir web build`: verify and build the UI.
- `uv build`: build wheel and sdist artifacts.

## Engineering rules

- Keep dependency direction aligned with `ARCHITECTURE.md`; core code must depend on ports, not concrete HTTP, tool, CLI, or server adapters.
- Preserve the Session v3, JSONL, REST, and WebSocket contracts. Treat wire-shape changes as public API changes.
- Put approval checks before side effects. Cancellation is cooperative and is not a rollback protocol.
- Add focused tests next to every behavior change. Never access real `~/.morrow` state from tests.
- Do not commit API keys, local `morrow.toml`, Session files, or generated virtual environments.

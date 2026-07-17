# Morrow Python Architecture

Morrow uses ports and adapters so the agent state machine is independent from model providers,
tools, persistence, CLI rendering, and the browser server.

```text
CLI / FastAPI server
        |
        v
runtime: turn orchestration, compaction, events, SessionStore, MCP assembly
        |
        v
core: Agent state machine, Model port, ToolRuntime port
      ^                              ^
      |                              |
model: OpenAI-compatible SSE         tools: built-ins + MCP
                                     |
                                     v
                                  sandbox

protocol and config are lower-level shared modules.
```

## Stable boundaries

- `morrow.protocol` owns messages, sessions, turns, permissions, approvals, and event types.
- `morrow.core` owns `Model`, `ToolRuntime`, `Agent`, `AgentTurn`, cancellation, and tool results.
- `morrow.model` maps OpenAI-compatible Chat Completions SSE to core model events.
- `morrow.tools` owns tool registration, built-in tools, patch transactions, shell execution, and MCP.
- `morrow.runtime` builds tools, compacts context, envelopes events, applies terminal turns, and persists sessions.
- `morrow.cli` and `morrow.server` are composition roots and must not duplicate the core loop.

## Turn invariants

- A completed turn appends all messages generated during that turn to the active model thread.
- A failed turn remains in audit history but does not alter the active model thread.
- Tool results are returned to the model in original call order, even when safe calls execute concurrently.
- Approval is resolved by request ID and must occur before file or shell side effects.
- Context compaction rebuilds active context but never deletes historical turn records.

## Compatibility

The Python implementation reads Rust-era Session documents with schema versions 1, 2, and 3,
and writes schema v3. The browser API and JSONL event envelope remain compatible with the Rust
v0.2.0 dashboard.

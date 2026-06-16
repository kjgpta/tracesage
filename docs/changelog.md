# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.2.0] — 2026-06-15

### Added

#### MCP tool-source attribution
- `tracesage.adapters.mcp.register_mcp_client(tracer, client)` loads a
  `MultiServerMCPClient`'s tools and attributes each to its originating MCP server
  (works around langchain-mcp-adapters not exposing provenance); plus
  `register_mcp_tools()` for explicit lists and `TraceSage.register_tool_source()`
- Tool events carry an `mcp_server` field, persisted on the event and in a new
  `mcp_tools` table (schema v3) so a server's tools — including uncalled ones —
  appear in the topology and `GET /api/tools` inventory even in `serve` mode
- UI: MCP server nodes, agent→server and server→tool edges, per-server colour
  rings/chips on tool nodes, a draggable, collapsible **"Tools by source"** panel,
  and a dynamic legend
- New optional extra `tracesage[mcp]` (langchain-mcp-adapters, mcp, langgraph),
  imported lazily — `import tracesage` never requires it

#### Developer experience
- **Kill switch**: `TRACESAGE_ENABLED=false` (or `enabled=False`) returns an inert
  tracer — no embedded server, no DB/worker, a no-op handler, near-zero overhead —
  so you can wire tracesage in once and disable it per-environment (e.g. in prod)
  without changing your integration
- **Embedded-server toggle**: `start_server` is a config field
  (`TRACESAGE_START_SERVER=false`) — capture traces in prod without running the
  in-process UI server; view them later with `tracesage serve`
- **Trace links**: a `🔍 tracesage: <url>` deep link prints on each new root run
  (`print_run_url` / `public_url` config; `TraceSage.run_url()`)
- **Zero-friction setup**: `with tracesage.trace()` (sync), `tracesage.start()`
  background runner, and `async with TraceSage.session(install=True)` — `install`
  registers a global LangChain handler so no `callbacks=` wiring is needed
- **Console + notebook renderers**: `tracesage show <run>` prints a terminal trace
  tree; `TraceSage.run_view()` renders the live UI inline in Jupyter
- **Richer errors**: exception type + full traceback captured on error events and
  retrievable in the UI drawer / `/full`
- **New CLI commands**: `demo`, `show`, `watch`, `diff`, `view`, and `serve --open`
- **pytest plugin**: the `tracesage_capture` fixture (auto-registered) with
  `assert_tool_called`, `assert_no_errors`, `total_tokens`, etc. — for both sync
  and async tests
- `TraceSage.flush()` to await full persistence (handy in tests/notebooks)
- In-UI within-run **search/filter** of the timeline

#### Examples
- Restructured `examples/` into `getting_started/` (no-key demos), `mcp/`
  (MCP attribution), and a new **`showcase/`** gallery — 30 real before/after
  apps across customer support, RAG, multi-agent, MCP, reasoning loops, and
  finance/legal/insurance verticals

### Fixed
- Auth middleware no longer 401s CORS preflight (`OPTIONS`) requests; CORS is the
  outermost layer
- WebSocket: per-socket send lock so catchup can't race a worker broadcast
- Worker: removed a double `task_done()` on the cancellation path
- Storage: timestamps normalized to fixed-width UTC so lexical ordering is
  monotonic (keyset pagination correctness)
- BlobStore: path-traversal guard now enforced on write as well as read
- Adapter: token counts of `0` no longer dropped; per-run caches guarded by a lock
- CLI `gc --max-blob-size-gb` no longer re-walks the whole blob tree per deletion;
  `export`/`import` no longer leave dangling `blob_path` references, and import
  synthesizes `runs` rows for nested sub-runs

### Changed
- Schema version 1 → 3 (additive, auto-migrated on `init()`; existing data preserved)

## [0.1.0] — 2026-05-02

### Added

#### Core pipeline
- LangChain `BaseCallbackHandler` integration for chain, agent, tool, LLM,
  chat-model, retriever events (`on_chain_start`, `on_chat_model_start`, etc.)
- `TraceSage.create()` factory wires storage + worker + server on a single event loop
- Async event queue with batched SQLite writes (50 events / 100 ms default)
- Gzipped blob storage for full event payloads (`*_END` events)
- Pluggable `StorageBackend` protocol (SQLite implementation in v0.1)

#### Server + UI
- FastAPI server with REST + WebSocket endpoints, lifespan-managed
- Endpoints: `/api/runs`, `/api/runs/{id}/journey`, `/api/runs/{id}/steps/{event_id}/full`,
  `/api/stats`, `/api/topology`, `/api/runs/{id}/export?format=jsonl`,
  `DELETE /api/runs/{id}`, `/ws/trace/{run_id}`, `/ws/runs`
- Single-page interactive dashboard with Cytoscape.js + dagre graph view
- Run list, timeline, step drawer, dark/light themes, keyboard shortcuts
- WebSocket reconnect with exponential backoff (1s → 30s)
- Replay mode for runs at 1x/2x/5x speed

#### CLI
- `tracesage serve` (read-only viewer)
- `tracesage export` (JSONL dump)
- `tracesage stats`
- `tracesage gc` (retention)
- `tracesage version`

#### Production safety
- Hard fail-stop when binding non-loopback addresses without `auth_token`
- Bearer-token HTTP auth (skip `/api/health`); constant-time comparison
- WebSocket auth via `?token=` query param or `Sec-WebSocket-Protocol` subprotocol
- Path-traversal guard in BlobStore
- Per-run event cap (circuit breaker) + root-level sampling
- Bounded LRU eviction on all internal run-id maps (no memory leaks in long runs)
- Worker FK auto-creation for sub-runs (handles nested LangGraph correctly)
- `queue.join()` waits for full persistence (not just dequeue)

#### Tests
- 77 unit + integration tests passing across all layers
- 4 viability test systems: order pipeline, research supervisor, parallel review, writer-critic loop
- 100-concurrent stress test (`tests/stress/`)
- Crash-recovery harness (`tools/crash_recovery_test.py`) verified ACID survival
- Bench tool (`tools/bench.py`) with cross-platform throughput numbers

#### Tooling
- GitHub Actions CI matrix: Linux/Windows/macOS × Python 3.11/3.12/3.13
- Trusted Publishing release workflow
- Ruff lint config

### Known limitations

- Centralized multi-producer mode planned for v0.2
- OpenInference / OpenTelemetry export planned for v0.3
- Cost tracking and PII redaction planned for v0.2
- CrewAI / AutoGen / LlamaIndex adapters planned for v0.4+

[0.2.0]: https://github.com/kjgpta/tracesage/releases/tag/v0.2.0
[0.1.0]: https://github.com/kjgpta/tracesage/releases/tag/v0.1.0

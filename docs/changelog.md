# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [0.1.1] — 2026-06-16

Branding, docs, and packaging polish — no runtime/behaviour changes.

### Added
- Brand logo assets under `assets/` — the topology `>` mark + `tracesage` wordmark,
  in dark / light / transparent and square-icon variants.
- The documentation site now carries the logo and favicon (MkDocs Material), and the
  README and docs home lead with the brand logo.

### Changed
- UI brand wordmark is now two-tone — `trace` in the accent blue, `sage` in green —
  echoing the logo's blue→green gradient (and it adapts to the light/dark theme).
- README badges modernized: dropped the legacy `.svg` suffixes (more reliable
  rendering across viewers), license pulled live from PyPI metadata, CI badge pinned
  to `main`, and a Docs badge added.

## [0.1.0] — 2026-06-16

First public release.

### Added

#### Core pipeline
- LangChain `BaseCallbackHandler` integration for chain, agent, tool, LLM,
  chat-model, retriever events (`on_chain_start`, `on_chat_model_start`, etc.)
- `TraceSage.create()` factory wires storage + worker + server on a single event loop
- Async event queue with batched SQLite writes (50 events / 100 ms default)
- Gzipped blob storage for full event payloads (`*_END` events)
- Pluggable `StorageBackend` protocol (SQLite implementation)

#### Server + UI
- FastAPI server with REST + WebSocket endpoints, lifespan-managed
- Endpoints: `/api/runs`, `/api/runs/{id}/journey`, `/api/runs/{id}/steps/{event_id}/full`,
  `/api/stats`, `/api/topology`, `/api/tools`, `/api/runs/{id}/export?format=jsonl`,
  `DELETE /api/runs/{id}`, `/ws/trace/{run_id}`, `/ws/runs`
- Single-page interactive dashboard with Cytoscape.js + dagre graph view
- Run list, timeline, step drawer, dark/light themes, keyboard shortcuts
- Within-run search/filter of the timeline
- WebSocket reconnect with exponential backoff (1s → 30s)
- Replay mode for runs at 1x/2x/5x speed

#### MCP tool-source attribution
- `tracesage.adapters.mcp.register_mcp_client(tracer, client)` loads a
  `MultiServerMCPClient`'s tools and attributes each to its originating MCP server
  (works around langchain-mcp-adapters not exposing provenance); plus
  `register_mcp_tools()` for explicit lists and `TraceSage.register_tool_source()`
- Tool events carry an `mcp_server` field, persisted on the event and in an
  `mcp_tools` table so a server's tools — including uncalled ones — appear in the
  topology and `GET /api/tools` inventory even in `serve` mode
- UI: MCP server nodes, agent→server and server→tool edges, per-server colour
  rings/chips on tool nodes, a draggable, collapsible **"Tools by source"** panel,
  and a dynamic legend
- Optional extra `tracesage[mcp]` (langchain-mcp-adapters, mcp, langgraph),
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
- **pytest plugin**: the `tracesage_capture` fixture (auto-registered) with
  `assert_tool_called`, `assert_no_errors`, `total_tokens`, etc. — for both sync
  and async tests
- `TraceSage.flush()` to await full persistence (handy in tests/notebooks)

#### CLI
- `tracesage serve` (read-only viewer), `export` (JSONL dump), `import`, `stats`,
  `runs`, `gc` (retention), `version`, `doctor`
- Developer commands: `demo`, `show`, `watch`, `diff`, `view`, and `serve --open`

#### Production safety
- Hard fail-stop when binding non-loopback addresses without `auth_token`
- Bearer-token HTTP auth (skip `/api/health`); constant-time comparison
- CORS preflight (`OPTIONS`) handled by the outermost CORS layer, never 401'd by auth
- WebSocket auth via `?token=` query param or `Sec-WebSocket-Protocol` subprotocol;
  per-socket send lock so catchup can't race a worker broadcast
- Path-traversal guard in BlobStore (enforced on read and write)
- Per-run event cap (circuit breaker) + root-level sampling
- Bounded LRU eviction on all internal run-id maps (no memory leaks in long runs)
- Worker FK auto-creation for sub-runs (handles nested LangGraph correctly)
- `queue.join()` waits for full persistence (not just dequeue)

#### Examples
- `examples/` organized into `getting_started/` (no-key demos), `mcp/`
  (MCP attribution), and a **`showcase/`** gallery — 30 real before/after apps
  across customer support, RAG, multi-agent, MCP, reasoning loops, and
  finance/legal/insurance verticals

#### Tooling
- GitHub Actions CI matrix: Linux/Windows/macOS × Python 3.11/3.12/3.13
- Trusted Publishing release workflow; MkDocs documentation site
- Ruff lint config
- Crash-recovery harness (`tools/crash_recovery_test.py`) and a throughput
  bench tool (`tools/bench.py`)

### Known limitations

- Centralized multi-producer mode planned for a future release
- OpenInference / OpenTelemetry export planned for a future release
- Cost tracking and PII redaction planned for a future release
- CrewAI / AutoGen / LlamaIndex adapters planned for a future release

[Unreleased]: https://github.com/kjgpta/tracesage/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/kjgpta/tracesage/releases/tag/v0.1.1
[0.1.0]: https://github.com/kjgpta/tracesage/releases/tag/v0.1.0

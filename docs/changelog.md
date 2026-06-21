# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Scoped topology.** The topology map and "Tools by source" panel now default to the
  **selected run** (latest when none) instead of an all-time union — so a removed tool,
  agent, or MCP server no longer lingers across app versions as you iterate. A toolbar
  selector offers *This run · Last N runs · All time*; `/api/topology` and `/api/tools`
  take a matching `?scope=run:<id>|last_n:<N>|all` param. Scoping also drops a removed
  MCP server's registered-but-uncalled tools (shown only if the server was active).

## [0.2.1] — 2026-06-21

Multi-app quality-of-life, plus honest positioning.

### Added
- **Project name in the UI.** Set `TRACESAGE_PROJECT_NAME` (config `project_name`) to
  label an app; it shows in the UI header and browser-tab title. Unset = nothing shown.
  Surfaced via `/api/health`. The bundled examples each set one so it's visible in their UI.
- **Auto-port.** If the configured port (default 7842) is busy, the embedded UI now
  auto-binds the next free port (scanning upward, then an OS-ephemeral port), so multiple
  apps run at once without a clash. Config `port_auto` (`TRACESAGE_PORT_AUTO`, default on);
  set `False` to pin exactly `port`. New `tracer.ui_url` property exposes the live URL;
  examples print the actual bound port. `tracesage serve` uses the same fallback.

### Changed
- **Repositioned as "local-first observability"** (was "production observability") across
  the README, docs, site description, package metadata, and CLI help — tracesage is a
  local-first dev tool that *bridges* to your production stack via OpenTelemetry, not a
  hosted production-monitoring service itself. Status messaging and the "Production" docs
  nav updated to match.

## [0.2.0] — 2026-06-18

The "full picture + production bridge" release: complete request/response capture,
OpenTelemetry export, per-application isolation, and a batch of UI/CLI/safety fixes.

### Added
- **OpenTelemetry (OTLP) export.** Set `otlp_endpoint` (env `TRACESAGE_OTLP_ENDPOINT`)
  and every event is also exported as an OTel span to a collector / Tempo / Jaeger /
  Datadog / Honeycomb, in addition to the local SQLite store. Maps `root_run_id`→trace,
  `run_id`→span, `parent_run_id`→parent, with token/error/MCP attributes. Optional
  `tracesage[otel]` extra; best-effort and never breaks the app if the collector is
  down. Config: `otlp_endpoint`, `otlp_service_name`, `otlp_headers`.
- **Full request *and* response payloads.** `*_start` events (inputs/prompts/queries)
  are now persisted as blobs alongside the existing `*_end` outputs, so the UI step
  drawer shows the request and response paired together for each step.
- **`tracesage show` colour + ordering** — each element kind (chain/agent/tool/llm/
  retriever) renders in its own colour, MCP-backed tools are tagged `mcp:<server>`,
  and `--reverse` flips sibling order (default follows execution flow).
- **`python -m tracesage`** now works (added `__main__`).
- **Positional args** for `tracesage export [RUN_ID]` and `import [INPUT]` (the
  `--run-id` / `--input` flags still work).
- Examples each write to their own data dir, demonstrating per-application isolation;
  new "Isolating multiple applications" docs.

### Changed
- **Topology + "Tools by source" isolation.** These are computed per data dir, so
  applications are kept separate by giving each its own `data_dir` (documented). All
  bundled examples now follow this.
- UI static assets are served with `Cache-Control: no-cache`, so UI updates take
  effect on a normal reload (no more stale cached `app.js`/`styles.css`).
- `tracesage runs --tag` is now filtered in SQL, so it composes correctly with
  `--limit`/`--offset` and the reported total.
- Removed the now-unused `aiofiles` runtime dependency.

### Fixed
- **Embedded UI server fails soft on a busy port.** A port conflict made uvicorn call
  `sys.exit(1)`, whose `SystemExit` previously crashed the *host application*; it is
  now contained — tracing continues without the local UI.
- Auth: a non-ASCII bearer token / `?token=` no longer raises (it failed open to a
  500); it fails closed with 401/4401.
- `BlobStore.read()` no longer blocks the event loop (decompress + parse offloaded).
- Timeline step cards: the kind label (CHAIN/TOOL/LLM/…) no longer overflows its box
  and overruns the timestamp.
- "Tools by source" panel: raised above the graph, clamped fully inside the pane, and
  re-clamped when a side pane expands (it slides over instead of being hidden).
- "Fit to view" in run-trace mode now fits the run instead of zooming out to include
  off-screen hidden nodes.

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
- Single-page interactive dashboard with a custom, hand-written SVG graph view
  (no JS framework, no build step) — auto-laid-out, hover/click/replay
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

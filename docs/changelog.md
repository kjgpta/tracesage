# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Raised the minimum versions of the runtime and dev dependencies to the current releases: FastAPI 0.165.0, Starlette 1.10.0, uvicorn 0.54.0, aiosqlite 0.22.1, Pydantic 2.14.4, pydantic-settings 2.16.3, Typer 0.29.2, websockets 17.4, and the matching floors for pytest, ruff, mypy, httpx, build, twine, and pip-audit.

## [1.0.0] — 2026-11-23

**First stable release.** From here on tracesage follows semantic versioning: the
public Python API, `TRACESAGE_*` settings, HTTP endpoints, CLI and data-dir format only
break in a major release, with a deprecation period first. See
[Stability and versioning](production.md#stability-and-versioning) for exactly what is
covered. Highlights: full traces of Claude Desktop, Claude Code, Cursor and Codex
sessions, support beyond LangChain through OpenTelemetry ingest (Pydantic AI,
OpenAI Agents SDK, CrewAI, OpenAI / Anthropic clients, any OTLP source), a manual span
API, conversation threads, human-in-the-loop interrupts, and refreshed docs media.

### Added
- **Full traces of AI-app sessions.** The collector reads each app's local
  session store and records every model request (named by the model, with
  thinking and tokens), every tool call, and every subagent nested under the
  tool call that spawned it. Sources: Claude Code transcripts and subagent
  transcripts, Cursor's `state.vscdb`, Codex rollouts (including `spawn_agent`
  children), Claude Desktop Cowork logs and sidebar chats. Past chats are imported
  on first start (Cursor and Codex: the last `TRACESAGE_IMPORT_DAYS` days, 30 by
  default). Hooks for an app whose store is read are ignored, so turns are not
  recorded twice.
- **Claude Desktop Chat and Cowork.** Sidebar chats whose transcript Desktop
  has saved in its cache or IndexedDB are imported; a chat that was never
  opened on this machine is not on disk. `tracesage connect claude-desktop --mcpb`
  builds a Desktop extension; the same `tracesage.mcpb` is attached to the
  GitHub release.
- **Threads mode.** Opening a thread shows the whole conversation, with an
  **Open run ↗** link on each turn that opens that turn's trace. Run trace,
  Waterfall and the timeline are hidden there, since a combined trace of a long
  chat is misleading; the right panel starts collapsed.
- **Sidebar search and filters.** The search box moved into the sidebar. Runs
  filter by source (each AI app, or "Your agents") and status and sort by newest,
  oldest, steps, tokens or cost; Threads filter by source and minimum turns and
  sort by activity, turns or tokens. Each tab keeps its own filters.
- **Resizable panes.** Drag the edges of the run list and the side panel (or use
  the arrow keys); double-click resets. Widths are remembered.
- **Loading state.** While the next run or thread loads, the middle pane shows a
  spinner instead of the previous item.
- **API.** `GET /api/runs` takes `sort` (`recent|oldest|steps|tokens|cost`) and
  `no_source`. `GET /api/threads` takes `sort` (`recent|oldest|turns|tokens`),
  `min_turns` and `no_source`, and `q` also matches step text.
  `GET /api/threads/{id}` takes `run=` and `last=N`. Topology and `/api/tools`
  accept `scope=thread:<id>`.
- **Docs.** An AI apps section: an overview of what you can do, step-by-step
  guides for Claude Desktop (including MCP), Claude Code, Cursor and Codex, a UI
  tour, and a privacy and data page.
- **Cost.** `llm_end` events store model, provider, cache-read and cache-write
  tokens, and `cost_usd` when the optional `tracesage[pricing]` extra is installed
  (`genai-prices`, offline, unknown models stay null and render as n/a). Runs
  carry `total_cost_usd`. The same numbers are in the UI, `tracesage runs`,
  JSONL export, the OpenTelemetry attribute `tracesage.cost_usd`, and
  `assert_cost_below` on the pytest fixture. `TRACESAGE_PRICE_OVERRIDES` replaces
  a rate. Core dependencies are unchanged; `tracesage[apps]` includes pricing.
- **Search.** An FTS5 index (`events_fts`) of names, summaries, errors and
  redacted payload text (capped, `search_index_payloads`). `GET /api/search`,
  `GET /api/runs?q=&model=&tool=&errors_only=`, a debounced search box, and
  `tracesage search`.
- **Waterfall.** `GET /api/runs/{id}/spans`, a Waterfall mode in the graph pane,
  and `tracesage show --waterfall`.
- **MCP server.** `tracesage mcp` (stdio, extra `tracesage[apps]`) exposes read
  tools plus `add_note`, `set_tags` and a gated `delete_run`, and the
  `debug_failed_run` prompt. Notes and tags have REST routes and the timeline
  shows notes.
- **Conversations.** `GET /api/threads` and `GET /api/threads/{id}`, a
  Runs | Threads toggle, a Conversation mode, and `tracesage thread`.
- **AI app connect.** `tracesage connect` / `disconnect` writes hooks so Claude
  Code, Codex (including ChatGPT desktop) and Cursor sessions show up in the UI,
  with a backup, a dry run, and `--metadata-only`. It does not install an MCP
  server unless you pass `--mcp`. `POST /v1/hooks/{app}` ingests those hooks
  (always `{}`). `POST /v1/logs` accepts Codex OTEL logs and discards them.
  `tracesage service install|start|stop|status` runs the collector at login.
  Claude Desktop chats and Cowork are recorded from local files (see above).
- **Schema v5.** Adds the cost and model columns, `run_notes`, `events_fts` and
  `runs.total_cost_usd`. Existing databases migrate in place and
  `tracesage doctor` asks for `tracesage reindex` so payload text and historical
  costs get filled in.
- **Beyond LangChain: OpenTelemetry ingest.** tracesage now accepts OpenTelemetry
  spans, so Pydantic AI, the OpenAI Agents SDK, CrewAI, the OpenAI and Anthropic
  clients — and any other OTel-instrumented code — get the same UI, CLI and pytest
  fixture. Three ways in (see `docs/frameworks.md`):
    - `tracesage.trace(instrument=["pydantic_ai"])` / `tracesage.instrument(...)`:
      in-process bridge, no network. New extras `tracesage[pydantic-ai]`,
      `[openai-agents]`, `[crewai]`, `[openai]`, `[anthropic]` pull the instrumentor;
      the framework itself is not pinned.
    - `POST /v1/traces` (OTLP/HTTP, protobuf or JSON, gzip/deflate) on the embedded
      server (config `ingest`, default on; `ingest_max_body_bytes` caps bodies) and on
      the new `tracesage serve --ingest` standalone collector. Gated by `auth_token`.
    - **Manual spans**: `tl.span(name, kind=...)` (sync/async context manager, nests via
      contextvars) and `@tl.trace_fn(...)`. LangChain roots nest under an active manual
      span. `tracesage.Span`, `tracesage.current_span` exported.
  Spans map onto runs by trace id with GenAI-semconv / OpenInference classification;
  event ids derive from span ids so exporter retries are idempotent. Events that
  arrived as OTel are not re-exported by `otlp_endpoint` (no loops). Runs now have an
  explicit `run_end` terminal event (a failed root span fails the run).
- The tracer no longer requires `langchain-core` to start: `tl.handler` / `tl.install()`
  raise or no-op with an install hint when it is missing, so OTel-only installs work.
- **Conversation threads.** The LangGraph `thread_id` (or `session_id` /
  `conversation_id` in callback metadata) is stored on each run. Filter with
  `GET /api/runs?thread_id=…`, `tracesage runs --thread …`, or the thread chip in
  the UI run list. Schema v4 adds `runs.thread_id`; existing databases migrate in place.
- **Human-in-the-loop interrupts.** A LangGraph `interrupt()` is recorded as a
  `chain_interrupt` event and the run gets the new `interrupted` status, instead
  of a `chain_error` on the node and a `completed` run. Works for interrupts raised
  in nodes, subgraphs and tools. Resuming starts a new run on the same thread.
- LangGraph checkpoint coordinates (`langgraph_node`, `langgraph_step`,
  `langgraph_checkpoint_ns`, …) and LangChain's `ls_provider` / `ls_model_name` are
  kept in each start event's payload under `metadata`. Other metadata keys are not stored.
- OTLP export follows the OpenTelemetry GenAI semantic conventions: span names
  `invoke_agent {name}`, `chat {model}`, `execute_tool {tool}`, `retrieval {name}`,
  plus `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.provider.name`,
  `gen_ai.tool.name`, `gen_ai.agent.name`, `gen_ai.conversation.id`, `error.type`
  and `mcp.method.name` for MCP-sourced tools.

### Changed
- A run opens on Run trace, with the Timeline on the right, instead of the
  Conversation view.
- The floating "Tools by source" panel is now a **Tools** tab in the right panel.
- The `connect claude-desktop --mcp` note no longer says Desktop chats are not
  recorded.
- Exported span names changed (were `chain {name}` / `tool {name}` / `llm {name}`);
  `tracesage.*` attributes are unchanged.
- LangGraph `ParentCommand` handoffs are recorded as a normal chain end, not an error.
- Docs screenshots, demo video and GIF regenerated from the current UI (now including
  an OTLP-ingested Pydantic AI run and a manual-span run). The repo's media shrank
  from ~45 MB to ~9 MB.

### Security
- Dependency floors no longer allow releases with known CVEs. Raised: `fastapi`
  0.133 (for `starlette` ≥ 1.3.1, now listed directly), `langchain-core` ≥ 1.3.3
  (cap `<2.0`, was `<1.5`), `langsmith` ≥ 0.8.18, `mcp` ≥ 1.30 and `<2`
  (2.x renamed FastMCP), `langgraph` ≥ 1.0.10,
  `langgraph-sdk` ≥ 0.4.6, OpenTelemetry ≥ 1.45.2, `protobuf` ≥ 5.29.6. `typer` ≥ 0.16
  and `langchain-mcp-adapters` ≥ 0.3 because older floors failed the test suite.
  This drops `langchain-core` 0.x support. A new `Dependencies` CI workflow runs
  `pip-audit` on the newest and the oldest allowed versions (and tests the oldest).
- `DELETE /api/runs/{id}` and `tracesage gc` could delete directories outside the
  blob store for a run id like `..` (reachable as `/api/runs/%2e%2e`). The blob
  store now rejects unsafe run ids on delete and size lookups.
- `cors_origins` now defaults to `[]` instead of `["*"]`, so other websites can no
  longer read traces from an unauthenticated local server. WebSocket handshakes
  from a foreign `Origin` are refused with `4403`.
- The UI sends the auth token in `Sec-WebSocket-Protocol` instead of `?token=`.
- `/ui/../…` paths no longer qualify for the static-shell auth exemption.
- Export `Content-Disposition` filenames are sanitized (no header injection).
- Chat message content is now flattened to plain JSON before redaction, so
  `redact_patterns` also applies to secrets inside message objects.
- `tracesage show` / `watch` strip terminal control sequences from trace text.

### Fixed
- "Your agents" could show an empty list: the source filter ran on one page of
  results in the browser. Filtering now happens on the server (`no_source`).
- Changing several filters quickly could show the results of an older request.
- A run opened from search or a link stays open when the list reloads without it.
- Model nodes now appear in the topology for hook-ingested model steps, which
  previously had no start event.
- Ingest slowed down as the database grew: every event upsert scanned the
  whole search index. Search rows now share the event's rowid (schema v6,
  migrated on start with indexed payload text kept), which made ingest about
  six times faster on a 75 MB database. New run rows in a batch are written in
  one transaction, and `/api/stats` reuses its blob-size scan for a minute.
- A process that exited on an uncaught exception without `stop()` could leave
  finished runs stuck as `running` (often with 1 step) or drop them. Cancelling the
  worker mid-batch discarded that batch after part of it was written; the batch is
  now replayed during shutdown, and run totals are recomputed from stored events.
- `tracesage.start()` / `trace()` ignored `TRACESAGE_START_SERVER=false` and always
  started the embedded server; they now default to the config value like `create()`.
- With an unreachable OTLP endpoint, `stop()` blocked for ~8s while the exporter
  retried; the final flush is now capped at 2s.
- `tracesage doctor` reported healthy nested runs as orphaned blob dirs, and only
  checked for a run's top-level blob folder. It now checks every blob file that
  events reference.
- The sync tracer returned by `start()` / `trace()` now has `ui_url` and
  `bound_port`, like the async one.
- UI: graph nodes with no LLM/tool calls beneath them (plain-Python nodes,
  human-approval nodes) were missing from the run-trace graph. A node that called
  `interrupt()` is now drawn with an amber dashed outline and a "PAUSED · INPUT" pill.
- UI: the step drawer closes when you select another run or filter its run out of
  the list, instead of showing the previous run's step.
- UI: run status is shown as a labelled pill (completed / failed / interrupted /
  running), not a colour-only dot; run rows get a visible keyboard focus ring.
- UI: below 900px the graph keeps a usable width, and below 760px the panes stack
  vertically (runs, graph, timeline) instead of squeezing the graph to zero.
- `GET /` redirects to `/ui/`. With a token set it used to return 404.
- Docs: `/docs`, `/redoc`, and `/openapi.json` require the bearer token when one
  is set. The README and quickstart snippets create the tracer. `export` is
  documented as omitting gzipped payloads, and `show` / `watch` / `diff` document
  `-d`. There is no TOML config file. The changelog compare links point at real tags.
- `stop()` dropped events still queued at shutdown; the worker now drains the
  queue before exiting, and `emit()` after `stop()` is ignored.
- A late event could flip a completed run back to `running` and reset its counters.
- Databases created before schema versioning failed every insert after upgrade
  (missing `mcp_server` column).
- `tracesage gc --max-runs 0` deleted every run; it now exits with an error.
- `serve --host 0.0.0.0` without a token prints a one-line error instead of a traceback.
- `watch` exits 1 for an unknown run; `show` / `watch` fall back to ASCII on
  consoles that can't encode the tree glyphs (e.g. Windows cp1252).
- UI: switching runs quickly could show the wrong run's journey; live topology
  refreshes ignored the selected scope; reconnect timers could stack.
- UI: in the run trace, tool labels overlapped the MCP source chip of the tool below.
- UI: selecting a run while a topology node was focused faded every node of the run.
- `@tl.trace_fn` on a nested function named the span `outer.<locals>.inner`; it now
  uses `inner` (methods keep `Class.method`).

### Changed
- Topology aggregation runs in SQL (memory no longer grows with event count).
- Journey and WebSocket catch-up return at most the 10,000 most recent steps,
  with a `truncated` flag.
- Callback handlers skip payload construction for sampled-out runs.

## [0.3.1] — 2026-07-01

A focused release that makes failed runs unmistakable in the UI and lets MCP tool
errors surface as real failures.

### Added
- **`register_mcp_client(..., handle_tool_errors=False)`.** New keyword on the MCP
  adapter (`tracesage.adapters.mcp`). By default (`True`) an MCP tool that errors
  server-side returns the error as tool content — the model sees it and can recover,
  matching langchain-mcp-adapters' behaviour. Pass `False` to make MCP tool errors
  **raise** instead, so a broken tool call fails the run and shows up as a red error
  node on the exact tool. Mirrors `ToolNode(handle_tool_errors=...)` for local tools.
  The loader now binds tools to the server's connection config (not a transient
  session) so each call opens its own session — fixing a `ClosedResourceError` when
  errors propagate.

### Changed
- **Failed nodes render solid red.** In the topology / run-trace graph, a node that
  errored is now filled solid `--error` red with a matching red glow — not just a red
  outline. The error styling overrides the trace-mode success/step glows, so a broken
  node reads unambiguously as red instead of blending toward orange.
- **MCP server palette recoloured.** The per-server colour palette (`mcpServerColor`)
  no longer includes red-family hues, so a healthy MCP server/tool is never confused
  with a failed (red) node. Red is now reserved exclusively for errors.

### Fixed
- **`register_mcp_client` across langchain-mcp-adapters API variants.** The tool
  loader falls back cleanly (`get_tools(server_name=...)` → connection config →
  per-server session) and no longer leaks a closed session.
- **Example `11_supervisor_research_team`** no longer raises `KeyError: 'next'` — the
  supervisor's stop branch now always emits a routing key.

## [0.3.0] — 2026-06-23

A major UI overhaul (topology vs. run-trace, step-through replay, readable
multi-server graphs), two real-world MCP example apps, and accurate per-call
token attribution.

### Added
- **Topology vs. Run-trace views.** A single graph pane now toggles between two
  layouts: **Topology** (the all-up system architecture, nodes in per-kind
  columns) and **Run trace** (one selected run laid out as a left → right call
  tree in call order). Selecting a run opens its trace; the Topology button
  returns to the architecture view.
- **Step-through replay.** Explicit **Start / Pause / Resume** controls plus
  **Prev / Next** manual stepping walk a run's execution on the graph. Start
  always restarts from the beginning; Resume only works once paused. Clicking a
  timeline step during replay pauses and jumps the cursor to that event.
- **Run-status toasts.** The UI pops a toast when a run you're watching finishes —
  "Run completed" (or "Run failed: …" with the error) — driven by the `/ws/runs`
  lifecycle feed, so you don't have to keep eyes on the run list.
- **Per-call token usage on LLM nodes.** Open an LLM node to see total **Tokens
  in / Tokens out** and a "Total tokens across N calls" line in the hero, summed
  per logical call (counted once, from each call's end event).
- **Gmail + YouTube demo** (`examples/mcp/gmail_youtube_demo/`) — a ReAct agent
  that reads a real Gmail inbox, pulls YouTube transcripts from linked videos, and
  summarises them. Ships `before.py` / `after.py` so the minimal tracesage diff (a
  couple of imports, the setup calls, and a `callbacks=` kwarg) is obvious. Gmail is optional
  (needs Google Application Default Credentials); without it the agent runs with
  YouTube only. `credentials.json` / `token.json` are gitignored.
- **Trip Planner demo** (`examples/mcp/trip_demo/`) — a single agent planning a trip
  across **three** MCP servers (flights, weather, hotels, 7 tools each) plus a local
  formatting tool, showcasing per-server tool-source attribution in the topology and
  the "Tools by source" panel. Bundles the three stdio MCP servers — no external
  installs, just an LLM key. Loads a repo-root `.env` automatically.
- `examples/mcp/requirements.txt` pinning the demos' dependencies.

### Changed
- **Readable graphs at scale.** MCP servers get distinct colours; a server's tools
  are grouped and tinted by source. A large tool fan-out wraps into multiple
  sub-columns, and the auto-layout no longer shrinks every node to fit — it caps
  the zoom (~72% floor) and pans/scrolls instead, so nodes stay legible. The
  explicit "fit" button still zooms to show everything.
- **Detailed timeline + node-drawer invocations.** The timeline keeps one card per
  event; node and edge drawers now group a call's start (request) + end (response)
  into **one logical invocation**, so counts and token totals are never doubled.
- **Step drawer is phase-aware.** Opening a `*_start` shows only its **request**;
  opening a `*_end` / `*_error` shows the full completed call (request + response).
- **Connection liveness.** The `/ws/runs` feed sends periodic heartbeats and the
  client tracks liveness, so the header reflects a dropped connection instead of
  showing a stale "connected".
- UI polish: plain `START` / `END` flags (no glyphs), full run id shown beside the
  Timeline heading, full-height collapse gutters mirroring the expand rails, and
  the topology-scope selector + "Tools by source" panel hidden in Run-trace mode.

### Fixed
- **Token attribution.** `llm_end` events now carry the model name, so token usage
  (which lives on the end event) is attributed to the LLM node — previously the
  end events were nameless and dropped from the node's invocation set.
- **Demo ergonomics.** Both MCP demos print the **actual** bound UI port (auto-port
  may pick 7843+ if 7842 is busy), preflight their prerequisites (MCP servers / LLM
  API key) and exit with clear setup instructions instead of cryptic failures.

## [0.2.1] — 2026-06-21

Multi-app quality-of-life, scoped topology, an offline-capable UI, and an
onboarding/docs overhaul (honest positioning included).

### Added
- **Project name in the UI.** Set `TRACESAGE_PROJECT_NAME` (config `project_name`) to
  label an app; it shows in the UI header and browser-tab title. Unset = nothing shown.
  Surfaced via `/api/health`. The bundled examples each set one so it's visible in their UI.
- **Auto-port.** If the configured port (default 7842) is busy, the embedded UI now
  auto-binds the next free port (scanning upward, then an OS-ephemeral port), so multiple
  apps run at once without a clash. Config `port_auto` (`TRACESAGE_PORT_AUTO`, default on);
  set `False` to pin exactly `port`. New `tracer.ui_url` property exposes the live URL;
  examples print the actual bound port. `tracesage serve` uses the same fallback.
- **Scoped topology.** The topology map and "Tools by source" panel now default to the
  **selected run** (latest when none) instead of an all-time union — so a removed tool,
  agent, or MCP server no longer lingers across app versions as you iterate. A toolbar
  selector offers *This run · Last N runs · All time* (with an editable **N**); selecting
  an older run shows *that* run's structure. `/api/topology` and `/api/tools` take a
  matching `?scope=run:<id>|last_n:<N>|all` param. Scoping also drops a removed MCP
  server's registered-but-uncalled tools (shown only if the server was active).
- **Troubleshooting / FAQ page** (`docs/troubleshooting.md`): "where are my runs?"
  (data-dir isolation), UUIDv7 run-id prefixes, stale-topology scoping, and the
  install/port gotchas — linked from the nav, quickstart, and README.
- **A complete, no-API-key runnable example** in the quickstart (uses `FakeListChatModel`),
  so first-time users aren't assumed to already have an agent/graph wired up.
- **Documented API object shapes** (`Run` / `StoredEvent` / `Stats` / `Topology` /
  `WSMessage`) field-by-field in the API reference, plus a note on the interactive
  `/docs` + `/openapi.json` endpoints.

### Changed
- **Repositioned as "local-first observability"** (was "production observability") across
  the README, docs, site description, package metadata, and CLI help — tracesage is a
  local-first dev tool that *bridges* to your production stack via OpenTelemetry, not a
  hosted production-monitoring service itself. Status messaging and the "Production" docs
  nav updated to match.
- **The UI now works fully offline.** PicoCSS is vendored into the package
  (`ui/vendor/pico.min.css`) instead of being fetched from a CDN, so the dashboard loads
  with zero network calls — matching the "no external services" promise.
- **Install commands quote the extra** (`pip install "tracesage[langchain]"`) throughout
  the docs and the adapter's `ImportError`, so the first command doesn't fail under zsh
  (the macOS default shell) with `no matches found`.
- **Onboarding points at the printed URL** rather than a hardcoded `:7842`, since
  auto-port may land elsewhere; added a per-app `data_dir` tip to the quickstart.
- **Roadmap items are no longer pinned to specific version numbers** in the docs (they
  read as stale promises if dates slip); framework support now clearly states
  LangChain/LangGraph is the only shipped adapter today.

### Fixed
- **Dropdown chevron restored on every `<select>`** (runs status filter, replay speed,
  topology scope) — a global `background` shorthand had been wiping PicoCSS's native arrow.
- **"Tools by source" is hidden in Run-trace mode** (it's a topology-level view) and
  shown again in Topology.
- **The timeline opens at the first step** of a selected run instead of jumping to the
  last; live tailing still re-engages when you scroll to the bottom.
- **The "Last N" scope input matches the scope `<select>`'s size**, and the runs status
  filter no longer sits underneath the panel's collapse button.

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

[Unreleased]: https://github.com/kjgpta/tracesage/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/kjgpta/tracesage/compare/v0.3.1...v1.0.0
[0.3.1]: https://github.com/kjgpta/tracesage/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kjgpta/tracesage/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/kjgpta/tracesage/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kjgpta/tracesage/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/kjgpta/tracesage/releases/tag/v0.1.1
[0.1.0]: https://github.com/kjgpta/tracesage/releases/tag/v0.1.0

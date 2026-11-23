<p align="center">
  <img src="assets/tracesage-logo-badge.png" alt="tracesage" width="420">
</p>

# tracesage

**See what your agents, and the AI assistants you use every day, actually did.**
Local-first observability for LangChain, LangGraph and OpenTelemetry agents, and
for Claude Desktop, Claude Code, Cursor and Codex sessions. Everything stays on
your machine.

These steps are for tracesage 1.0.0.

## Try it in 30 seconds

```bash
pip install tracesage          # demo needs no LangChain extra
tracesage demo                 # seeds a sample run and opens the live UI
```

No API key. No config. No code. `tracesage demo` opens a real trace so you can
click around the graph right away. To trace your own agent:

```python
import tracesage

with tracesage.trace():          # starts the local UI and captures this process
    result = agent.invoke("your input")
```

(`pip install "tracesage[langchain]"` first. The [quickstart](quickstart.md) has
the async form and a copy-pasteable example that needs no API key.)

### Or record the AI assistants you already use

```bash
pip install "tracesage[apps]"
tracesage connect claude-code    # or: cursor, codex, claude-desktop, or no name for all
```

Restart the app if it was open (in Codex, also run `/hooks` once and trust the
hooks). Open [http://127.0.0.1:7842](http://127.0.0.1:7842) and set **Source** to
that app. Sessions already on disk are imported, and the next prompt you send
shows up as a run: each model call, tool call and subagent, with inputs, outputs,
timing and tokens. [Connect each app →](ai-apps.md)

<table>
  <tr>
    <td width="50%"><img src="assets/ui-apps-run-trace.png" alt="A Claude Code turn as a run trace with its model calls, tool calls and a subagent"></td>
    <td width="50%"><img src="assets/ui-apps-conversation.png" alt="A whole chat in the Threads tab with an Open run link on each turn"></td>
  </tr>
</table>

[Get started →](quickstart.md){ .md-button .md-button--primary }
[Browse examples →](examples.md){ .md-button }

---

## Why tracesage

LangChain agents emit a rich callback stream — chain start/end, tool start/end,
LLM start/end, retrieval, errors. tracesage captures all of it without
changing your workflow logic, persists it locally (SQLite + gzipped blobs),
and renders it in an interactive graph + timeline UI in real time.

- **Zero infrastructure.** No Docker. No Postgres. No external services. `pip install`.
  The UI is fully self-contained (assets vendored, no CDN) and works offline.
- **Minimal-change integration.** Add one callback to your existing `ainvoke` — no rewrites.
- **Crash-safe by design.** The handler never raises and the tracer never crashes
  your pipeline.
- **Interactive graph view.** Custom SVG graph (no framework), auto-laid-out. Hover, click, replay any run.
- **MCP-aware.** Tools loaded from MCP servers are attributed by source — see which tools
  came from which server vs. which are hardcoded. See [MCP support](mcp.md).
- **OpenTelemetry export.** Optionally ship every trace as OTel spans to a collector /
  Tempo / Jaeger / Datadog / Honeycomb — the bridge to your production stack. See
  [Configuration → OpenTelemetry export](configuration.md).
- **Pluggable storage.** SQLite and gzipped blobs behind a `StorageBackend` protocol, so another backend can be added. See [Extending](extending.md).
- **Apache 2.0 licensed.** Free forever.

## Where to go next

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Quickstart](quickstart.md)**

    Install, run an example, open the UI. Five minutes.

-   :material-graph: **[Concepts](concepts.md)**

    What `agent`, `tool`, `llm`, `retriever`, `chain`, and `mcp` mean — read
    this first if you want to interpret a topology.

-   :material-robot: **[AI apps](ai-apps.md)**

    Record Claude Desktop, Claude Code, Cursor and Codex sessions as full traces,
    and ask Claude about them over MCP.

-   :material-cog: **[Configuration](configuration.md)**

    Every `TRACESAGE_*` env var explained.

-   :material-shield-check: **[Deploying & hardening](production.md)**

    Auth, sampling, retention, the safety rails, and the OTel bridge to your stack.

-   :material-book-open-variant: **[Examples](examples.md)**

    30 before/after apps across popular use cases. Pick the closest match to
    your architecture and copy the integration.

-   :material-console: **[CLI reference](cli.md)**

    `tracesage serve` / `export` / `stats` / `runs` / `gc`.

-   :material-puzzle: **[Extending](extending.md)**

    Adding framework adapters and storage backends.

</div>

## What you'll see

<table>
  <tr>
    <td width="50%"><img src="assets/ui-topology.png" alt="Live topology — agents, MCP servers, tools, and LLM nodes colour-coded by kind"></td>
    <td width="50%"><img src="assets/ui-run-list.png" alt="Run list — green completed runs, red failed runs at a glance"></td>
  </tr>
  <tr>
    <td align="center"><em>Live topology — agents, MCP servers, tools and LLMs, colour-coded by kind.</em></td>
    <td align="center"><em>Run list — every run at a glance. <strong>Green = completed, red = failed.</strong></em></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/ui-run-trace.png" alt="Run trace — execution path with step-by-step timeline and replay"></td>
    <td width="50%"><img src="assets/ui-failed-run.png" alt="Failed run — the broken tool node highlighted in red, with the exact error"></td>
  </tr>
  <tr>
    <td align="center"><em>Run-trace — the exact path a run took, with timeline and step-through replay.</em></td>
    <td align="center"><em>When something breaks, a <strong>red node</strong> pins the exact tool and error.</em></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/ui-waterfall.png" alt="Latency waterfall for one agent run"></td>
    <td width="50%"><img src="assets/ui-conversation.png" alt="Conversation view of a multi-turn thread"></td>
  </tr>
  <tr>
    <td align="center"><em>Waterfall — the slow step is the long bar.</em></td>
    <td align="center"><em>Conversation — a thread reads like the chat that produced it.</em></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/ui-otel-run.png" alt="Pydantic AI run ingested over OpenTelemetry"></td>
    <td width="50%"><img src="assets/ui-manual-spans.png" alt="Plain-Python run traced with manual spans"></td>
  </tr>
  <tr>
    <td align="center"><em>Any <strong>OpenTelemetry</strong> source — here a Pydantic AI agent via <code>serve --ingest</code>.</em></td>
    <td align="center"><em>Plain Python with <code>tl.span</code> / <code>@tl.trace_fn</code> — agent, retriever, tool and LLM spans.</em></td>
  </tr>
</table>

Once a run lands, the UI shows:

- **Run list** — every run with status, tags, started-at, total steps, total tokens,
  and a toast when a watched run completes or fails
- **Graph pane** — toggles between four views:
    - **Topology** — agent / tool / chain / retriever relationships by kind, scoped to the
      selected run by default (a toolbar selector switches to last-N-runs / all-time)
    - **Run-trace** — one selected run laid out as a left → right call tree in call order
    - **Waterfall** — one bar per span, so the slow step is obvious
    - **Conversation** — the thread as chat, which is the default for a coding-agent session
- **Timeline** — chronological steps; a `*_start` shows its request, a `*_end` the full
  **request + response** payloads (MCP-backed tools are tagged with their server)
- **Step-through replay** — explicit Start / Pause / Resume plus Prev / Next manual
  stepping (1x / 2x / 5x); clicking a timeline step jumps the cursor there
- **Node inspector** — click any node for its stats; LLM nodes show token usage (in / out)

Keyboard: `j` / `k` next/prev run, `/` focus search, `t` theme, `Esc` close, `?` help.

### Header stats

The top bar shows live health at a glance (plus the optional
[project name](configuration.md) next to the brand):

| Chip | Meaning |
|---|---|
| **ev/s** | Trace events received per second, as a **1-minute rolling average** (events in the last 60 s ÷ 60). A quick pulse that capture is flowing — it rises while a run is active and decays to 0 when idle. |
| **running** | How many **root runs are currently in progress** (`runs_active` from `/api/stats`; falls back to counting `running` rows in the list). |
| **dropped** | Events **dropped because the ingestion queue was full** (backpressure). Should stay **0**; the chip turns red if not. If it climbs, lower `sample_rate` or raise `queue_maxsize` — see [Deploying & hardening → what to monitor](production.md). |
| **connection dot** | The live **WebSocket link** to the server: *connected* (updates streaming in real time), *connecting*, or *disconnected* (it auto-reconnects with backoff). |

`ev/s` is computed in the browser from the event stream; `running` and `dropped`
come from [`GET /api/stats`](api.md), polled periodically.

### Watch a trace stream in

<video controls muted playsinline width="100%" poster="assets/ui-topology.png">
  <source src="assets/tracesage-demo.mp4" type="video/mp4">
  Your browser can't play embedded video —
  <a href="assets/tracesage-demo.mp4">download the demo clip</a> instead.
</video>

### Inspect any node

Click a node to open its drawer — counts, durations, errors, the tools it provides
or uses, and (for MCP) its server of origin. **LLM** nodes show token usage
(in / out, total across calls).

<table>
  <tr>
    <td width="50%"><img src="assets/ui-llm-drawer.png" alt="LLM inspector with token usage"></td>
    <td width="50%"><img src="assets/ui-mcp-drawer.png" alt="MCP server inspector"></td>
  </tr>
  <tr>
    <td align="center"><em>LLM inspector — token usage (in / out, total across calls) and latency.</em></td>
    <td align="center"><em>MCP server inspector — provided tools, invocations and callers.</em></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/ui-tools-by-source.png" alt="Tools tab"></td>
    <td width="50%"><img src="assets/ui-step-payload.png" alt="Step request and response payloads"></td>
  </tr>
  <tr>
    <td align="center"><em>The Tools tab — tools grouped by origin (MCP vs. local).</em></td>
    <td align="center"><em>Any step → its full request and response payloads.</em></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/ui-agent-drawer.png" alt="Agent inspector"></td>
    <td width="50%"><img src="assets/ui-chain-drawer.png" alt="Chain inspector"></td>
  </tr>
  <tr>
    <td align="center"><em>Agent inspector — invocations, latency, what it calls and who calls it.</em></td>
    <td align="center"><em>Chain inspector — the orchestration layer and the agents it drives.</em></td>
  </tr>
</table>

## Status

**1.0.0 (2026-11-23) is the first stable release.** tracesage follows
[semantic versioning](https://semver.org); see
[Stability and versioning](production.md#stability-and-versioning) for which surfaces are
covered. Supported: LangChain / LangGraph natively, Pydantic AI, the OpenAI Agents SDK,
CrewAI and the OpenAI / Anthropic clients through OpenTelemetry, any OTLP source via
`tracesage serve --ingest`, plain Python via [manual spans](frameworks.md), and the
sessions of Claude Desktop, Claude Code, Cursor and Codex via `tracesage connect`.
Built for local development and single-node tracing; for a central backend, export
over OpenTelemetry (see [Production](production.md)). See the
[changelog](changelog.md) for release notes.

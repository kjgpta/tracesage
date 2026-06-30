<div align="center">

<img src="docs/assets/tracesage-logo-badge.png" alt="tracesage" width="420">

# tracesage

**See what your LangChain & LangGraph agents actually did.**
Local-first observability — drop in one line, watch every run live in your browser.

[![PyPI](https://img.shields.io/badge/pypi-v0.3.0-3775A9)](https://pypi.org/project/tracesage/)
[![Python versions](https://img.shields.io/pypi/pyversions/tracesage)](https://pypi.org/project/tracesage/)
[![License: MIT](https://img.shields.io/pypi/l/tracesage)](LICENSE)
[![CI](https://github.com/kjgpta/tracesage/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kjgpta/tracesage/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue)](https://kjgpta.github.io/tracesage/)
[![Status: beta](https://img.shields.io/badge/status-beta-blue)](#status)

<a href="docs/assets/tracesage-demo.mp4"><img src="docs/assets/tracesage-demo.gif" alt="tracesage UI — topology, run trace, token usage, and a failed run pinpointed to the exact tool" width="100%"></a>

<sub><a href="docs/assets/tracesage-demo.mp4">▶ Watch the full 1-minute demo</a></sub>

</div>

### Try it in 30 seconds

```bash
pip install "tracesage[langchain]"
tracesage demo        # seeds a sample run and opens the live UI in your browser
```

No API key, no config, no code — `tracesage demo` opens a real trace so you can click
around the graph immediately. Then add **one line** to your own agent:

```python
result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tracer.handler]},   # ← the only line you add
)
# tracesage prints a link: 🔍 http://localhost:7842/ui  — open it to watch the run live
```

### When a run breaks, see exactly where

A failed tool call shows up as a **red node on the exact step that broke** — the tool,
its input, and the error — instead of a stack trace buried in your logs.

<div align="center">
<img src="docs/assets/ui-failed-run.png" alt="A failed run: the look_up_order tool node is red, pinpointing exactly where and why the run broke" width="90%">
</div>

---

## Contents

- [Why tracesage](#why-tracesage)
- [Install](#install)
- [Quick start](#quick-start)
- [Screenshots](#screenshots)
- [Concepts: topology node kinds](#concepts-topology-node-kinds)
- [Features](#features)
- [Examples](#examples)
- [Documentation](#documentation)
- [Comparison](#comparison)
- [Performance](#performance)
- [Status](#status)
- [Contributing](#contributing)
- [License](#license)

---

## Why tracesage

LangChain agents emit a rich callback stream — chain start/end, tool start/end,
LLM start/end, retrieval, errors. **tracesage** captures all of it without
changing your workflow logic, persists it locally (SQLite + gzipped blobs),
and renders it in an interactive graph + timeline UI in real time.

- **Zero infrastructure.** No Docker. No Postgres. No external services. `pip install`.
  The UI is fully self-contained (assets vendored, no CDN) and works offline.
- **Minimal-change integration.** Add one callback to your existing `ainvoke` — no rewrites.
- **Crash-safe by design.** The handler never raises and the tracer never crashes
  your pipeline.
- **Interactive graph view.** Custom SVG graph (no framework), auto-laid-out. Hover, click, replay any run.
- **MCP-aware.** Tools loaded from MCP servers are attributed by source, so you can
  see which tools came from which server vs. which are hardcoded. See [docs/mcp.md](docs/mcp.md).
- **OpenTelemetry export.** Optionally ship every trace as OTel spans to your collector /
  Tempo / Jaeger / Datadog / Honeycomb — the bridge from the local dev view to your
  production stack (config-driven; see [docs/configuration.md](docs/configuration.md#opentelemetry-export)).
- **Multi-app friendly.** Run several apps at once: each auto-binds a free port (7842, 7843, …),
  shows its `TRACESAGE_PROJECT_NAME` in the UI header, and keeps its own data dir — no clashes.
- **Pluggable storage.** SQLite today; Postgres / remote-collector / object-store backends planned (see [`production_roadmap.md`](production_roadmap.md)).
- **MIT licensed.** Free forever.

## Install

```bash
pip install "tracesage[langchain]"
```

Requires **Python 3.11+**. Quote the extra (`"tracesage[langchain]"`) so zsh —
the default macOS shell — doesn't glob the brackets and fail with `no matches
found`. The `[langchain]` extra pulls `langchain-core`; that's the only mandatory
third-party dep beyond the standard FastAPI / aiosqlite / pydantic stack. If your
app uses **LangGraph**, also `pip install langgraph` (tracesage doesn't pull it).

tracesage is **provider-agnostic** — it traces LangChain's callback stream, so
OpenAI / Anthropic / local models are all captured automatically; there's no
provider setting in tracesage. Install whichever provider you use:

```bash
pip install langchain-openai      # OPENAI_API_KEY=...
pip install langchain-anthropic   # ANTHROPIC_API_KEY=...
```

For MCP tool-source attribution (loads tools from MCP servers and tags them by
source), install the `mcp` extra:

```bash
pip install 'tracesage[mcp]'
```

## Quick start

**See it in 5 seconds** — seed a sample trace and open the UI:

```bash
tracesage demo
```

**Sync scripts / notebooks** — wrap your code; every LangChain call is captured
automatically (no `callbacks=` wiring) and a clickable trace link is printed:

```python
import tracesage

with tracesage.trace() as tl:          # starts the UI + global capture
    result = agent.invoke("your input")     # 🔍 tracesage: http://127.0.0.1:7842/ui/#run=...
    input("Trace ready — open the printed link, then Enter to exit.")  # keep the UI up
```

(The embedded UI stops when the `with` block / process exits; traces persist to
`~/.tracesage`, so you can also reopen them later with `tracesage serve`.)

**Async apps** — use the context manager (or `await TraceSage.create()` for full control):

```python
import asyncio
from tracesage import TraceSage

async def main():
    async with TraceSage.session(install=True) as tl:   # install=True → global capture
        await graph.ainvoke({"input": "your payload"}, config={"tags": ["my-system"]})
        await tl.flush()                                 # ensure events are persisted
        print(tl.run_url("<run_id>"))                    # deep link to a run

asyncio.run(main())
```

Prefer explicit wiring? Pass `config={"callbacks": [tl.handler]}` instead of `install=True`.

That's it. Open the URL tracesage prints on startup (`🔍 tracesage: http://…/ui/#run=…`)
and explore — it's **http://localhost:7842/ui** by default, but auto-port picks the next
free port (`7843`, …) if `7842` is taken, so trust the printed link (`tracer.ui_url`).

### Developer workflow

Once you have traces, debug them without leaving your terminal:

```bash
tracesage show <run_id>          # render a run as a tree in the terminal
tracesage watch <run_id>         # live-tail events as they stream
tracesage diff <run_a> <run_b>   # compare two runs (tokens, tools, errors)
tracesage view trace.jsonl       # open an exported trace in the UI directly
```

**Test your agents** — the `tracesage_capture` pytest fixture is auto-registered:

```python
def test_agent_uses_search(tracesage_capture):
    agent.invoke("find me a hotel")
    tracesage_capture.assert_tool_called("search")
    tracesage_capture.assert_no_errors()
    assert tracesage_capture.total_tokens()[0] < 5000
```

See **[`docs/development.md`](docs/development.md)** for the full developer guide, and
**[`examples/showcase/`](examples/showcase/)** for 30 before/after apps across popular use cases.

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/assets/ui-topology.png" alt="Live topology graph"></td>
    <td width="50%"><img src="docs/assets/ui-run-list.png" alt="Run list — completed and failed runs"></td>
  </tr>
  <tr>
    <td align="center"><em>Live topology — agents, tools, LLMs and MCP servers across a run.</em></td>
    <td align="center"><em>Run list — every run at a glance: <strong>green completed, red failed</strong>.</em></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/assets/ui-run-trace.png" alt="Run trace and timeline"></td>
    <td width="50%"><img src="docs/assets/ui-step-payload.png" alt="Step request and response payloads"></td>
  </tr>
  <tr>
    <td align="center"><em>Run trace — the path a run took, with a step-by-step timeline you can replay.</em></td>
    <td align="center"><em>Click any step for its full <strong>request and response</strong> payloads, tokens and errors.</em></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/assets/ui-llm-drawer.png" alt="LLM inspector with token usage"></td>
    <td width="50%"><img src="docs/assets/ui-tools-by-source.png" alt="Tools by source panel"></td>
  </tr>
  <tr>
    <td align="center"><em>Inspect an <strong>LLM</strong> — token usage (in / out, total across calls) and latency.</em></td>
    <td align="center"><em>“Tools by source” — every tool grouped by origin (MCP servers vs. local).</em></td>
  </tr>
</table>

## Concepts: topology node kinds

When you open the UI, every node in the topology graph is one of five event-based
kinds — plus a synthesized **`mcp`** node when you attribute tools to an MCP server.
Knowing what each means is the prerequisite to reading a trace:

| Kind | What it is | Examples you'll see |
|---|---|---|
| **`agent`** | A function **you** registered as a LangGraph node, that calls other components | `agent:billing_agent`, `agent:fact_extractor`, `agent:supervisor` |
| **`tool`** | A side-effect function (DB query, API call, calculation) decorated with `@tool` | `tool:lookup_account`, `tool:run_sql`, `tool:cite_sources` |
| **`llm`** | A language-model call (chat or completion) | `llm:FakeListChatModel`, `llm:ChatOpenAI`, `llm:ChatAnthropic` |
| **`retriever`** | A `BaseRetriever` subclass — the "R" in RAG | `retriever:Chroma`, `retriever:FAISS`, `retriever:_FixedCorpusRetriever` |
| **`chain`** | Plumbing — LCEL primitives, the LangGraph orchestrator, routing functions | `chain:LangGraph`, `chain:RunnableSequence`, `chain:ChatPromptTemplate`, `chain:route_after_quality` |
| **`mcp`** | An MCP server (synthesized) — groups the tools loaded from it | `mcp:weather`, `mcp:math`, `mcp:github` |

Quick mental model:

- **`agent`** is your code that *calls* something. It does reasoning.
- **`tool`** does the actual side-effect work and returns a result.
- **`llm`** is what you count, cost, and cache.
- **`retriever`** is its own dimension — "did we find the right docs?"
  is a different question from "did the LLM use them well?".
- **`chain`** is the wrapping machinery (the `prompt | llm | parser`
  pipe operator, the LangGraph state machine, routing functions). It's
  infrastructure, not business logic.
- **`mcp`** groups tools by the MCP server they came from — provenance, so you can
  tell server-provided tools from your hardcoded ones (see [docs/mcp.md](docs/mcp.md)).

Read the full reference at **[`docs/concepts.md`](docs/concepts.md)** —
it covers how tracesage classifies events into kinds, why agents with no
descendants get demoted to `chain`, and walks through a research-pipeline
topology piece by piece.

## Features

### Live interactive UI

- **Run list** with status badges (running / completed / failed), search, status filter,
  and a **toast** when a watched run finishes ("Run completed" / "Run failed: …")
- **Topology vs. Run-trace** — one graph pane toggles between the all-up system
  architecture (nodes in per-kind columns) and a single run laid out as a left → right
  call tree in call order. Picking a run opens its trace; the Topology button returns.
- **Step-through replay** — explicit **Start / Pause / Resume** plus **Prev / Next**
  manual stepping walk a run on the graph (1x / 2x / 5x); clicking a timeline step during
  replay pauses and jumps the cursor there
- **Scoped topology** — the graph + "Tools by source" default to the *selected run*
  (a toolbar selector switches to last-N-runs / all-time), so removed tools, agents, or
  MCP servers don't linger across app versions as you iterate
- **MCP attribution** — `mcp:` server nodes with **distinct per-server colors** (tools
  tinted by source), agent→server and server→tool edges, and a draggable "Tools by source"
  panel; a large tool fan-out wraps into columns and the graph caps its zoom so nodes stay
  legible instead of shrinking to fit
- **Timeline** with click-to-expand step cards — a `*_start` shows its **request**, a
  `*_end` the full **request + response** payloads, plus tokens, duration, and errors;
  MCP-backed tools are tagged with their server
- **Node inspector** — click any node for its stats; LLM nodes show **token usage**
  (in / out, total across N calls) and each node's invocations are grouped one-per-call
- **Header stats** — `ev/s` (1-min rolling event rate), `running` (in-progress runs),
  `dropped` (events lost to backpressure; red if non-zero), and a heartbeat-backed
  connection dot that reflects a dropped link instead of going stale
- **Dark / light themes**, persisted in `localStorage`
- **Keyboard shortcuts:** `j`/`k` next/prev run, `/` focus search, `t` toggle theme, `Esc`, `?`

### Production safety

- Refuses to bind non-loopback addresses without `auth_token` (hard fail-stop)
- Bearer-token HTTP auth + WebSocket auth via `?token=` or subprotocol
- Constant-time token comparison
- Path-traversal blocked at runtime
- Per-run event cap (circuit breaker) and root-level sampling for high volumes
- Bounded internal state (no unbounded memory growth in long-running processes)
- **Kill switch:** `TRACESAGE_ENABLED=false` makes it a complete no-op (no server, no
  DB/worker, no-op handler) — integrate once, disable per-environment. See
  [docs/production.md](docs/production.md).

### OpenTelemetry export (bridge to production)

tracesage's own UI/CLI is the **local developer-loop** view. To get agent traces into a
**production observability stack**, point `otlp_endpoint` at any OTLP/HTTP collector —
every trace is *also* emitted as OpenTelemetry spans (the local SQLite store stays too):

```bash
pip install "tracesage[otel]"
export TRACESAGE_OTLP_ENDPOINT=http://localhost:4318      # or set in TraceSageConfig
```

```python
from tracesage import TraceSageConfig
# pass to TraceSage.create() / .session() / tracesage.trace():
cfg = TraceSageConfig(otlp_endpoint="http://localhost:4318", otlp_service_name="my-agent")
```

**Where it helps:** the spans land in whatever OTLP-compatible backend you already run —
**Grafana Tempo, Jaeger, Datadog, Honeycomb, Arize/Phoenix, an OTel Collector** — so agent
traces sit alongside the rest of your services (correlated with HTTP/DB spans, long-term
retention, alerting, multi-service/multi-process views) with **no vendor lock-in**. Mapping:
`root_run_id`→trace, `run_id`→span, `parent_run_id`→parent; tokens/errors/MCP server become
span attributes. Best-effort — if the collector is down, tracing continues and your app is
never affected.

> **Note — it's config-driven, not a UI toggle.** There's no button in tracesage's UI to
> turn this on. You enable it via config/env *before* tracing starts, and the exported
> traces appear in **your OTel backend's** UI (Grafana/Jaeger/Datadog/…), not in
> tracesage's own UI. tracesage's UI always shows the local SQLite view. See
> [docs/configuration.md](docs/configuration.md#opentelemetry-export).

### CLI

The `tracesage` command ships with the package — a **viewer + utilities** over a
data directory. It never ingests events; ingestion happens only when your code calls
`TraceSage.create()`. The fastest way to see it working, with zero code:

```bash
tracesage demo                                    # seed a sample trace, serve + open the UI
```

**Inspect traces in the browser:**

| Command | What it does |
|---|---|
| `tracesage serve -d ~/.tracesage [--open]` | Read-only viewer over an existing data dir (auto-picks the next free port; `--auth-token` to gate it) |
| `tracesage view trace.jsonl [--open]` | Open an exported JSONL trace directly in the UI |

**Inspect traces in the terminal (no server):**

| Command | What it does |
|---|---|
| `tracesage runs --status failed --limit 20` | List root runs (filter by `--status` / `--tag`, `--json` for NDJSON) |
| `tracesage show <run_id>` | Render a run as a colour-coded indented call tree (MCP tools tagged `mcp:<server>`) |
| `tracesage watch <run_id>` | Live-tail a run's events as they're written (`--once` to print and exit) |
| `tracesage diff <run_a> <run_b>` | Compare two runs side by side — status, steps, tokens, tools, errors |
| `tracesage stats [--json]` | Summary stats — run counts by status, avg duration, token totals, DB size |

**Move, retain, diagnose:**

| Command | What it does |
|---|---|
| `tracesage export [RUN_ID] --all -o trace.jsonl` | Dump one run or `--all` to JSONL (`-o -` for stdout) |
| `tracesage import -i trace.jsonl -d DIR` | Read a JSONL export back into a data dir (backups / move between machines) |
| `tracesage gc --max-runs 10000 [--dry-run]` | Retention — delete oldest runs/blobs beyond a count or `--max-blob-size-gb` |
| `tracesage doctor -d ~/.tracesage` | Read-only diagnostics — schema version, run counts, orphan/missing-blob checks |
| `tracesage version` | Print the installed version |

Most commands take `--data-dir, -d` (default `~/.tracesage`). See
[`docs/cli.md`](docs/cli.md) for the full per-command reference.

## Examples

The [`examples/`](examples/) directory has three tiers:

- **[`getting_started/`](examples/getting_started/)** — 3 standalone demos driven by
  `FakeListChatModel` (**no API key**): smart-search agent, research supervisor, RAG.
- **[`mcp/`](examples/mcp/)** — MCP attribution demos (needs `tracesage[mcp]`), including
  two real-world before/after apps:
    - **[`trip_demo/`](examples/mcp/trip_demo/)** — one agent over **three** bundled stdio
      MCP servers (flights / weather / hotels, 7 tools each) + a local tool; **no external
      installs**, just an LLM key. Best place to see multi-server topology + "Tools by source".
    - **[`gmail_youtube_demo/`](examples/mcp/gmail_youtube_demo/)** — a ReAct agent that reads
      a real Gmail inbox and summarises YouTube transcripts (YouTube needs no auth; Gmail is
      optional, via Google ADC).
- **[`showcase/`](examples/showcase/)** — **30 real before/after apps** across popular use
  cases (customer support, RAG, multi-agent, MCP, reasoning loops, finance/legal/insurance).
  Each ships a plain `before.py` and an `after.py` with tracesage added, so `diff` shows the
  exact integration.

```bash
# instant, no key:
python examples/getting_started/01_smart_search_agent.py       # then open http://localhost:7842/ui

# the real-world gallery (needs an LLM key):
pip install -r examples/showcase/requirements.txt
export OPENAI_API_KEY=...
python examples/showcase/01_support_faq_router/after.py
```

The **[showcase gallery](examples/showcase/)** has 30 before/after apps spanning
LangChain + LangGraph: routing, parallel fan-out, the supervisor pattern, RAG variants,
writer-critic loops, map-reduce, MCP, self-correction, and finance/legal/insurance verticals.

## Documentation

| Doc | What's in it |
|---|---|
| [Quickstart](docs/quickstart.md) | First trace in five minutes |
| [Developer guide](docs/development.md) | Trace links, sync/notebook setup, CLI debugging, pytest fixture |
| [Configuration](docs/configuration.md) | Every `TRACESAGE_*` env var explained |
| [CLI reference](docs/cli.md) | All `tracesage` subcommands |
| [Production guide](docs/production.md) | Sampling, auth, retention, deployment |
| [Troubleshooting](docs/troubleshooting.md) | "Where are my runs?", install/port issues, FAQ |
| [Comparison](docs/comparison.md) | tracesage vs LangSmith / LangFuse / Phoenix |
| [Extending tracesage](docs/extending.md) | Adding framework adapters and storage backends |
| **[Examples](examples/showcase/)** | **30 before/after apps with tracesage added** |

## Comparison

When to use `tracesage` vs alternatives:

| | tracesage | LangSmith | LangFuse | Phoenix |
|---|---|---|---|---|
| Zero infra | ✓ | cloud / enterprise self-host | Docker + Postgres | ✓ |
| Pure pip install | ✓ | ✓ (cloud) | ✗ | ✓ |
| Live UI | ✓ | ✓ | ✓ | ✓ |
| MIT licensed | ✓ | proprietary | MIT | Elastic v2 |
| Eval framework | non-goal | ✓ | ✓ | ✓ |
| OpenTelemetry export | ✓ (0.2+) | partial | ✓ | ✓ |

See [`docs/comparison.md`](docs/comparison.md) for the full breakdown.

## Performance

Bench results (5,000 events, 100 distinct run_ids, 20% blob-eligible):

| Platform | Sustained | p99 write | Drops |
|---|---|---|---|
| Linux x86 + NVMe | 800–1200 ev/s | 80–150 ms | 0 |
| Windows NTFS | 60–100 ev/s | 1–2 s | 0 |

Windows NTFS is the bottleneck (gzip + fsync amplification). For very high
throughput on Windows, raise `TRACESAGE_WORKER_BATCH_SIZE` to 200 and
`TRACESAGE_WORKER_BATCH_TIMEOUT` to 0.5.

## Status

**Beta.** API may still shift before v1.0. The PyPI badge at the top shows the published
version; it's stamped by the release workflow **when a version actually ships** (so it
matches PyPI and never gets ahead of a release). Built for local development and
single-process tracing; centralized multi-process / remote-collector mode is
on the roadmap (see [`production_roadmap.md`](production_roadmap.md)). Today the **only
shipped adapter is LangChain / LangGraph** — the core is framework-neutral and
CrewAI / AutoGen / LlamaIndex adapters are planned, not yet available.

See [the changelog](docs/changelog.md) for release notes.

## Contributing

Issues and pull requests are welcome.

- Read [`docs/contributing.md`](docs/contributing.md) before sending a PR
- For non-trivial changes, open a [discussion](https://github.com/kjgpta/tracesage/discussions/categories/ideas) first
- Bugs and feature requests use the [issue templates](https://github.com/kjgpta/tracesage/issues/new/choose)
- Security reports: see [`SECURITY.md`](.github/SECURITY.md) — please don't open public issues for vulnerabilities
- All participation is governed by the [Code of Conduct](.github/CODE_OF_CONDUCT.md)

## License

[MIT](LICENSE) © tracesage contributors

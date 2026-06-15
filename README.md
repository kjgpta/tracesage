<div align="center">

# tracelens

**Production observability for LangChain & LangGraph multi-agent systems.**
Drop in two lines, see live execution traces in your browser.

[![PyPI version](https://img.shields.io/pypi/v/tracelens.svg)](https://pypi.org/project/tracelens/)
[![Python versions](https://img.shields.io/pypi/pyversions/tracelens.svg)](https://pypi.org/project/tracelens/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/kjgpta/tracelens/actions/workflows/ci.yml/badge.svg)](https://github.com/kjgpta/tracelens/actions/workflows/ci.yml)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

</div>

```python
from tracelens import TraceLens

# Illustrative only — `await` runs inside an async function. See Quick start
# below for a complete, runnable `async def main()`.
tracer = await TraceLens.create()                          # one-time setup

result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tracer.handler]},                # only line you add
)

# Open http://localhost:7842/ui to see the trace live
```

---

## Contents

- [Why tracelens](#why-tracelens)
- [Install](#install)
- [Quick start](#quick-start)
- [Concepts: the five topology kinds](#concepts-the-five-topology-kinds)
- [Features](#features)
- [Examples](#examples)
- [Documentation](#documentation)
- [Comparison](#comparison)
- [Performance](#performance)
- [Status](#status)
- [Contributing](#contributing)
- [License](#license)

---

## Why tracelens

LangChain agents emit a rich callback stream — chain start/end, tool start/end,
LLM start/end, retrieval, errors. **tracelens** captures all of it without
changing your workflow logic, persists it locally (SQLite + gzipped blobs),
and renders it in an interactive graph + timeline UI in real time.

- **Zero infrastructure.** No Docker. No Postgres. No external services. `pip install`.
- **Two-line integration.** One callback added to your existing `ainvoke`.
- **Production-grade safety.** The handler never raises. The tracer never crashes
  your pipeline.
- **Interactive graph view.** Custom SVG graph (no framework), auto-laid-out. Hover, click, replay any run.
- **MCP-aware.** Tools loaded from MCP servers are attributed by source, so you can
  see which tools came from which server vs. which are hardcoded. See [docs/mcp.md](docs/mcp.md).
- **Pluggable storage.** SQLite today; Postgres / remote-collector / object-store backends planned (see [`production_roadmap.md`](production_roadmap.md)).
- **MIT licensed.** Free forever.

## Install

```bash
pip install tracelens[langchain]
```

Requires **Python 3.11+**. The `[langchain]` extra pulls `langchain-core`;
that's the only mandatory third-party dep beyond the standard FastAPI /
aiosqlite / pydantic stack.

Optional extras for real LLM providers:

```bash
pip install langchain-openai langchain-anthropic
```

For MCP tool-source attribution (loads tools from MCP servers and tags them by
source), install the `mcp` extra:

```bash
pip install 'tracelens[mcp]'
```

## Quick start

**See it in 5 seconds** — seed a sample trace and open the UI:

```bash
tracelens demo
```

**Sync scripts / notebooks** — wrap your code; every LangChain call is captured
automatically (no `callbacks=` wiring) and a clickable trace link is printed:

```python
import tracelens

with tracelens.trace() as tl:          # starts the UI + global capture
    result = agent.invoke("your input")     # 🔍 tracelens: http://127.0.0.1:7842/ui/#run=...
```

**Async apps** — use the context manager (or `await TraceLens.create()` for full control):

```python
import asyncio
from tracelens import TraceLens

async def main():
    async with TraceLens.session(install=True) as tl:   # install=True → global capture
        await graph.ainvoke({"input": "your payload"}, config={"tags": ["my-system"]})
        await tl.flush()                                 # ensure events are persisted
        print(tl.run_url("<run_id>"))                    # deep link to a run

asyncio.run(main())
```

Prefer explicit wiring? Pass `config={"callbacks": [tl.handler]}` instead of `install=True`.

That's it. Open **http://localhost:7842/ui** and explore.

### Developer workflow

Once you have traces, debug them without leaving your terminal:

```bash
tracelens show <run_id>          # render a run as a tree in the terminal
tracelens watch <run_id>         # live-tail events as they stream
tracelens diff <run_a> <run_b>   # compare two runs (tokens, tools, errors)
tracelens view trace.jsonl       # open an exported trace in the UI directly
```

**Test your agents** — the `tracelens_capture` pytest fixture is auto-registered:

```python
def test_agent_uses_search(tracelens_capture):
    agent.invoke("find me a hotel")
    tracelens_capture.assert_tool_called("search")
    tracelens_capture.assert_no_errors()
    assert tracelens_capture.total_tokens()[0] < 5000
```

See **[`docs/development.md`](docs/development.md)** for the full developer guide, and
**[`examples/showcase/`](examples/showcase/)** for 30 before/after apps across popular use cases.

## Concepts: the five topology kinds

When you open the UI, every node in the topology graph is one of five
kinds. Knowing what each one means is the prerequisite to reading a trace:

| Kind | What it is | Examples you'll see |
|---|---|---|
| **`agent`** | A function **you** registered as a LangGraph node, that calls other components | `agent:billing_agent`, `agent:fact_extractor`, `agent:supervisor` |
| **`tool`** | A side-effect function (DB query, API call, calculation) decorated with `@tool` | `tool:lookup_account`, `tool:run_sql`, `tool:cite_sources` |
| **`llm`** | A language-model call (chat or completion) | `llm:FakeListChatModel`, `llm:ChatOpenAI`, `llm:ChatAnthropic` |
| **`retriever`** | A `BaseRetriever` subclass — the "R" in RAG | `retriever:Chroma`, `retriever:FAISS`, `retriever:_FixedCorpusRetriever` |
| **`chain`** | Plumbing — LCEL primitives, the LangGraph orchestrator, routing functions | `chain:LangGraph`, `chain:RunnableSequence`, `chain:ChatPromptTemplate`, `chain:route_after_quality` |

Quick mental model:

- **`agent`** is your code that *calls* something. It does reasoning.
- **`tool`** does the actual side-effect work and returns a result.
- **`llm`** is what you count, cost, and cache.
- **`retriever`** is its own dimension — "did we find the right docs?"
  is a different question from "did the LLM use them well?".
- **`chain`** is the wrapping machinery (the `prompt | llm | parser`
  pipe operator, the LangGraph state machine, routing functions). It's
  infrastructure, not business logic.

Read the full reference at **[`docs/concepts.md`](docs/concepts.md)** —
it covers how tracelens classifies events into kinds, why agents with no
descendants get demoted to `chain`, and walks through example 02's
topology piece by piece.

## Features

### Live interactive UI

- **Run list** with status badges (running / completed / failed), search, status filter
- **SVG graph** showing agents, tools, and execution paths — pulses as events arrive
- **Timeline** with click-to-expand step cards, lazy-loaded full payloads
- **Replay** mode that re-animates a run at 1x / 2x / 5x speed
- **Dark / light themes**, persisted in `localStorage`
- **Keyboard shortcuts:** `j`/`k` next/prev run, `/` focus search, `t` toggle theme, `Esc`, `?`

### Production safety

- Refuses to bind non-loopback addresses without `auth_token` (hard fail-stop)
- Bearer-token HTTP auth + WebSocket auth via `?token=` or subprotocol
- Constant-time token comparison
- Path-traversal blocked at runtime
- Per-run event cap (circuit breaker) and root-level sampling for high volumes
- Bounded internal state (no unbounded memory growth in long-running processes)
- **Kill switch:** `TRACELENS_ENABLED=false` makes it a complete no-op (no server, no
  DB/worker, no-op handler) — integrate once, disable per-environment. See
  [docs/production.md](docs/production.md).

### CLI

```bash
tracelens serve  --data-dir ~/.tracelens          # read-only viewer
tracelens export --run-id RUN_ID -o trace.jsonl   # export to JSONL
tracelens import -i trace.jsonl                   # import a JSONL export
tracelens stats  --data-dir ~/.tracelens          # summary stats
tracelens runs   --status failed --limit 20       # list runs
tracelens gc     --max-runs 10000                 # retention cleanup
tracelens doctor --data-dir ~/.tracelens          # data-dir diagnostics
tracelens version
```

See [`docs/cli.md`](docs/cli.md) for full reference.

## Examples

The [`examples/`](examples/) directory has three tiers:

- **[`getting_started/`](examples/getting_started/)** — 3 standalone demos driven by
  `FakeListChatModel` (**no API key**): smart-search agent, research supervisor, RAG.
- **[`mcp/`](examples/mcp/)** — tools from 2 local MCP servers + 2 hardcoded tools,
  attributed by source in the topology (needs `tracelens[mcp]`).
- **[`showcase/`](examples/showcase/)** — **30 real before/after apps** across popular use
  cases (customer support, RAG, multi-agent, MCP, reasoning loops, finance/legal/insurance).
  Each ships a plain `before.py` and an `after.py` with tracelens added, so `diff` shows the
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
| [Configuration](docs/configuration.md) | Every `TRACELENS_*` env var explained |
| [CLI reference](docs/cli.md) | All `tracelens` subcommands |
| [Production guide](docs/production.md) | Sampling, auth, retention, deployment |
| [Comparison](docs/comparison.md) | tracelens vs LangSmith / LangFuse / Phoenix |
| [Extending tracelens](docs/extending.md) | Adding framework adapters and storage backends |
| **[Examples](examples/showcase/)** | **30 before/after apps with tracelens added** |

## Comparison

When to use `tracelens` vs alternatives:

| | tracelens | LangSmith | LangFuse | Phoenix |
|---|---|---|---|---|
| Zero infra | ✓ | cloud / enterprise self-host | Docker + Postgres | ✓ |
| Pure pip install | ✓ | ✓ (cloud) | ✗ | ✓ |
| Live UI | ✓ | ✓ | ✓ | ✓ |
| MIT licensed | ✓ | proprietary | MIT | Elastic v2 |
| Eval framework | non-goal | ✓ | ✓ | ✓ |
| OpenTelemetry export | v0.3+ | partial | ✓ | ✓ |

See [`docs/comparison.md`](docs/comparison.md) for the full breakdown.

## Performance

Bench results (5,000 events, 100 distinct run_ids, 20% blob-eligible):

| Platform | Sustained | p99 write | Drops |
|---|---|---|---|
| Linux x86 + NVMe | 800–1200 ev/s | 80–150 ms | 0 |
| Windows NTFS | 60–100 ev/s | 1–2 s | 0 |

Windows NTFS is the bottleneck (gzip + fsync amplification). For very high
throughput on Windows, raise `TRACELENS_WORKER_BATCH_SIZE` to 200 and
`TRACELENS_WORKER_BATCH_TIMEOUT` to 0.5.

## Status

**v0.2 — alpha.** API may still shift before v1.0. Production-monitoring-ready for
single-Python-process deployments; centralized multi-process / remote-collector mode
is on the roadmap (see [`production_roadmap.md`](production_roadmap.md)).

See [`CHANGELOG.md`](CHANGELOG.md) for release notes.

## Contributing

Issues and pull requests are welcome.

- Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before sending a PR
- For non-trivial changes, open a [discussion](https://github.com/kjgpta/tracelens/discussions/categories/ideas) first
- Bugs and feature requests use the [issue templates](https://github.com/kjgpta/tracelens/issues/new/choose)
- Security reports: see [`SECURITY.md`](.github/SECURITY.md) — please don't open public issues for vulnerabilities
- All participation is governed by the [Code of Conduct](.github/CODE_OF_CONDUCT.md)

## License

[MIT](LICENSE) © tracelens contributors

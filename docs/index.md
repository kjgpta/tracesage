# tracelens

**Production observability for LangChain & LangGraph multi-agent systems.**
Drop in two lines, see live execution traces in your browser.

```python
from tracelens import TraceLens

tracer = await TraceLens.create()                          # one-time setup

result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tracer.handler]},                # only line you add
)

# Open http://localhost:7842/ui to see the trace live
```

[Get started in 5 minutes →](quickstart.md){ .md-button .md-button--primary }
[See the integration guide →](integration_guide/README.md){ .md-button }

---

## Why tracelens

LangChain agents emit a rich callback stream — chain start/end, tool start/end,
LLM start/end, retrieval, errors. tracelens captures all of it without
changing your workflow logic, persists it locally (SQLite + gzipped blobs),
and renders it in an interactive graph + timeline UI in real time.

- **Zero infrastructure.** No Docker. No Postgres. No external services. `pip install`.
- **Two-line integration.** One callback added to your existing `ainvoke`.
- **Production-grade safety.** The handler never raises. The tracer never crashes
  your pipeline.
- **Interactive graph view.** Cytoscape.js + dagre layout. Hover, click, replay any run.
- **Pluggable storage.** SQLite in v0.1; Postgres / remote-server / JSONL backends planned.
- **MIT licensed.** Free forever.

## Where to go next

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Quickstart](quickstart.md)**

    Install, run an example, open the UI. Five minutes.

-   :material-graph: **[Concepts](concepts.md)**

    What `agent`, `tool`, `llm`, `retriever`, and `chain` mean — read
    this first if you want to interpret a topology.

-   :material-cog: **[Configuration](configuration.md)**

    Every `TRACELENS_*` env var explained.

-   :material-shield-check: **[Production](production.md)**

    Auth, sampling, retention, monitoring, multi-tenant deployments.

-   :material-book-open-variant: **[Integration guide](integration_guide/README.md)**

    10 multi-agent systems in `before/`-`after/` form. Pick the closest match
    to your architecture and copy the pattern.

-   :material-console: **[CLI reference](cli.md)**

    `tracelens serve` / `export` / `stats` / `runs` / `gc`.

-   :material-puzzle: **[Extending](extending.md)**

    Adding framework adapters and storage backends.

</div>

## What you'll see

Once a run lands, the UI shows:

- **Run list** — every run with status, tags, started-at, total steps, total tokens
- **Topology graph** — agent / tool / chain / retriever relationships across runs
- **Timeline** — chronological steps with click-to-expand full payloads
- **Replay** — animate any completed run at 1x / 2x / 5x

Keyboard: `j` / `k` next/prev run, `/` focus search, `t` theme, `Esc` close, `?` help.

## Status

**v0.1 — alpha.** API may shift before v1.0. Production-monitoring-ready for
single-Python-process deployments. Centralized multi-process server mode comes
in v0.2. See the [changelog](changelog.md) for release notes.

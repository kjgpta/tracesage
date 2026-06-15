# Quickstart

Run any LangChain or LangGraph workflow with full observability in under a minute.

## Install

```bash
pip install tracelens[langchain]
```

`tracelens` requires Python 3.11+ and pulls `langchain-core` as the only mandatory
external dependency for the LangChain adapter.

## See it in 5 seconds

```bash
tracelens demo      # seeds a sample trace, opens the UI
```

## Integrate (the whole thing)

**Sync scripts / notebooks** — wrap your run; every LangChain call is captured
automatically (no `callbacks=` wiring), and a clickable trace link prints:

```python
import tracelens

with tracelens.trace():                 # starts the UI + global capture
    result = agent.invoke("your input")  # 🔍 tracelens: http://127.0.0.1:7842/ui/#run=...
```

**Async apps** — use the context manager (or `await TraceLens.create()` for full control):

```python
from tracelens import TraceLens

async with TraceLens.session(install=True) as tl:   # install=True → global capture
    result = await graph.ainvoke({"input": payload})
    await tl.flush()                                 # ensure events are persisted
```

Prefer explicit wiring (or finer control)? Skip `install=True` and pass the handler:

```python
result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tl.handler]},   # the only line you add
)
```

Open `http://localhost:7842/ui` in your browser to see the trace live as it runs.

## What you'll see

- **Run list** (left): every `ainvoke` you make, with status (running / completed / failed),
  step counts, and tags.
- **Graph** (center): an interactive topology of every agent and tool you've used,
  with edges traced by execution.
- **Timeline** (right): every callback event in order, expandable for the full LLM payload.

## Configuration

Override defaults via env vars or the config object:

```python
from tracelens import TraceLens, TraceLensConfig

cfg = TraceLensConfig(
    host="127.0.0.1",
    port=7842,
    sample_rate=0.1,           # capture 10% of runs in production
    per_run_event_cap=10_000,  # circuit breaker per run
)
tracer = await TraceLens.create(config=cfg)
```

See [configuration.md](configuration.md) for the full list.

## Multiple invocations

The tracer is created once at app startup. Reuse `tracer.handler` across every
invocation:

```python
tracer = await TraceLens.create()

for input_payload in incoming_requests:
    result = await graph.ainvoke(
        input_payload,
        config={"callbacks": [tracer.handler], "tags": ["api-v2"]},
    )
```

Each invocation creates a separate run in the dashboard. Tags propagate.

## Stopping cleanly

`TraceLens` registers an `atexit` cleanup, but for explicit shutdown:

```python
await tracer.stop()  # drains queue, closes DB, stops server
```

## Viewing data later

The UI is also available as a CLI viewer that does not ingest:

```bash
tracelens serve --data-dir ~/.tracelens
```

Useful for inspecting traces from a previous session, or running the viewer on
a different machine pointed at a synced data directory.

## Next steps

- [development.md](development.md) — trace links, sync/notebook setup, terminal debugging (`show`/`watch`/`diff`), the `tracelens_capture` pytest fixture
- [configuration.md](configuration.md) — every knob (including the `TRACELENS_ENABLED` kill switch)
- [production.md](production.md) — sampling, auth, retention, disabling, deployment patterns
- [cli.md](cli.md) — `serve`, `demo`, `show`, `watch`, `diff`, `view`, `export`, `stats`, `gc`
- [mcp.md](mcp.md) — attributing tools to their MCP server
- [comparison.md](comparison.md) — when to use tracelens vs LangSmith / Phoenix / LangFuse

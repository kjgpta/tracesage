# Quickstart

Run any LangChain or LangGraph workflow with full observability in under a minute.

## Install

```bash
pip install tracelens[langchain]
```

`tracelens` requires Python 3.11+ and pulls `langchain-core` as the only mandatory
external dependency for the LangChain adapter.

## Two-line integration

```python
from tracelens import TraceLens

tracer = await TraceLens.create()  # starts background worker + UI server

result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tracer.handler]},   # the only line you add to your code
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

- [configuration.md](configuration.md) — every knob
- [production.md](production.md) — sampling, auth, retention, deployment patterns
- [cli.md](cli.md) — `serve`, `export`, `stats`, `gc`
- [comparison.md](comparison.md) — when to use tracelens vs LangSmith / Phoenix / LangFuse

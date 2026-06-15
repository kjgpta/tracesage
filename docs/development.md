# Developer guide

tracelens is built to shorten the local debugging loop for LangChain / LangGraph
agents. This page covers the developer-facing features; for production deployment
see [`production_roadmap.md`](https://github.com/kjgpta/tracelens/blob/main/production_roadmap.md).

## Zero-friction setup

### Sync scripts & notebooks

```python
import tracelens

with tracelens.trace() as tl:        # starts the embedded UI + installs globally
    agent.invoke("your input")       # captured automatically — no callbacks= needed
    print(tl.run_url("<run_id>"))    # deep link into the UI
```

`tracelens.trace()` runs the tracer on a background thread, so it works in plain
synchronous code. `tracelens.start(...)` returns the same handle without the
context manager (call `.stop()` yourself).

### Async apps

```python
from tracelens import TraceLens

async with TraceLens.session(install=True) as tl:
    await graph.ainvoke({"input": "..."})
    await tl.flush()                 # block until events are persisted
```

- `install=True` registers the handler as a **global** LangChain callback, so every
  chain/agent/LLM/tool call is captured without threading `callbacks=[...]` through
  each invocation. Omit it and pass `config={"callbacks": [tl.handler]}` for explicit wiring.
- `await TraceLens.create()` gives you the tracer object directly (you call `.stop()`).

### Trace links in your console

The first time each root run is seen, tracelens prints a clickable link to stderr:

```
🔍 tracelens: http://127.0.0.1:7842/ui/#run=019ec...
```

Disable with `TraceLensConfig(print_run_url=False)`. Behind a proxy, set
`public_url="https://traces.example.com"` and links use that base.

## CLI for debugging

```bash
tracelens demo                   # seed a sample trace and open the UI (fastest first look)
tracelens serve -d ~/.tracelens --open   # view an existing data dir, open the browser
tracelens show <run_id>          # render a run as an indented tree in the terminal
tracelens watch <run_id>         # live-tail a run's events as they're written
tracelens diff <run_a> <run_b>   # compare two runs: status, steps, tokens, tools, errors
tracelens view trace.jsonl       # open an exported JSONL trace in the UI directly
```

`tracelens show` example:

```
Run 019ec…  completed  · 3 steps · 100 tokens
◇ chain research_agent  620ms
├─ ◯ llm gpt-4o-mini  210ms ↑42/↓58
└─ ▭ tool web_search  350ms
```

## Notebooks

`tl.run_view(run_id)` returns an object that renders the live UI inline in a Jupyter
cell (via an iframe):

```python
tl.run_view("019ec...")          # interactive trace, embedded in the notebook
```

## Testing your agents

Installing tracelens registers a pytest plugin exposing the `tracelens_capture`
fixture. It captures everything during a test into an isolated tracer (no server)
and offers query + assertion helpers:

```python
def test_agent_behaviour(tracelens_capture):
    agent.invoke("find me a hotel in paris")

    tracelens_capture.assert_tool_called("search")
    tracelens_capture.assert_no_errors()

    tokens_in, tokens_out = tracelens_capture.total_tokens()
    assert tokens_in < 5000

    assert "search" in tracelens_capture.tool_calls()
    assert tracelens_capture.runs()                 # at least one run captured
```

Works for both sync and `async def` tests. Read helpers auto-flush the pipeline, so
events from a just-completed `invoke`/`ainvoke` are immediately visible.

| Helper | Returns |
|---|---|
| `runs()` | captured `Run`s |
| `events(run_id=None)` | `StoredEvent`s (one run, or all) |
| `tool_calls()` | tool names invoked (in order) |
| `called_tool(name)` | bool |
| `errors()` | error events |
| `total_tokens()` | `(input, output)` summed |
| `assert_tool_called(name)` / `assert_no_errors()` / `assert_run_count(n)` | raise on mismatch |

## Richer error capture

When an agent step raises, tracelens stores the exception type **and full traceback**
on the error event (retrievable in the UI drawer's "Full payload" and via
`GET /api/runs/{run}/steps/{event}/full`), so you can debug failures after the fact.

## In-UI search

The timeline pane has a filter box: type to show only steps whose tool/agent/summary/
type matches, with a live match count.

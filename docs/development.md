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
    input("Trace ready — open the printed link, then press Enter to exit.")
```

`tracelens.trace()` runs the tracer on a background thread, so it works in plain
synchronous code. The embedded UI server stops when the `with` block / process exits, so
a one-shot script needs to stay alive (the `input(...)`) while you view the live trace;
traces also persist to `~/.tracelens` for later `tracelens serve`. `tracelens.start(...)`
returns the same handle without the context manager (call `.stop()` yourself). A
`🔍 tracelens: <url>` link prints to stderr on each new root run — that's the simplest way
to open a specific run; `tl.run_url(run_id)` builds the same link if you already hold a
LangChain run id.

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

The fixture installs tracelens as the **global** LangChain handler for the test (its own
temp `data_dir`, no server), so you do **not** add `callbacks=[...]` — a bare
`invoke()`/`ainvoke()` is captured automatically. Read helpers auto-flush the pipeline, so
events from a just-completed call are immediately visible.

**Two gotchas worth knowing:**

- **Async tests** need [`pytest-asyncio`](https://pytest-asyncio.readthedocs.io/) or they
  silently skip (a false green). Install it and set `asyncio_mode = "auto"`:
  ```toml
  # pyproject.toml
  [tool.pytest.ini_options]
  asyncio_mode = "auto"
  ```
- **`total_tokens()` reflects only usage the model reports.** `FakeListChatModel` (used by
  the no-key examples) reports none, so a token-budget assertion is vacuous against it —
  gate token tests behind a real provider (e.g. `@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), ...)`).
  See [`examples/showcase/26_llm_judge_eval/test_eval.py`](https://github.com/kjgpta/tracelens/blob/main/examples/showcase/26_llm_judge_eval/test_eval.py)
  for a complete CI example.

Works for both sync and `async def` tests.

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

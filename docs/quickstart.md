# Quickstart

Run any LangChain or LangGraph workflow with full observability in under a minute.

## Install

```bash
pip install tracesage[langchain]
```

`tracesage` requires Python 3.11+ and pulls `langchain-core` as the only mandatory
external dependency for the LangChain adapter. If your app uses **LangGraph**, also
`pip install langgraph` (tracesage doesn't pull it). tracesage is **provider-agnostic** —
it traces the LangChain callback stream, so OpenAI, Anthropic, local models, etc. are all
captured automatically; there is no provider setting in tracesage.

### Using a real provider

The examples use a fake model so they need no key. For your own app, install a provider
and set its key:

```bash
# OpenAI
pip install langchain-openai      && export OPENAI_API_KEY=...
# or Anthropic
pip install langchain-anthropic   && export ANTHROPIC_API_KEY=...
```

You construct the model exactly as you normally would (`ChatOpenAI(...)`,
`ChatAnthropic(...)`, or `init_chat_model("anthropic:claude-...")`) — tracesage captures it
whichever you choose.

## See it in 5 seconds

```bash
tracesage demo      # seeds a sample trace, opens the UI
```

## Integrate (the whole thing)

**Sync scripts / notebooks** — wrap your run; every LangChain call is captured
automatically (no `callbacks=` wiring), and a clickable trace link prints:

```python
import tracesage

with tracesage.trace():                 # starts the UI + global capture
    result = agent.invoke("your input")  # 🔍 tracesage: http://127.0.0.1:7842/ui/#run=...
    input("Trace ready — open the printed link, then press Enter to exit.")
```

The embedded UI server stops when the `with` block (and the process) exits, so a one-shot
script needs to stay alive while you look — hence the `input(...)`. (Traces also persist to
`~/.tracesage`, so you can always reopen them later with `tracesage serve`.)

**Async apps** — use the context manager (or `await TraceSage.create()` for full control):

```python
from tracesage import TraceSage

async with TraceSage.session(install=True) as tl:   # install=True → global capture
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
from tracesage import TraceSage, TraceSageConfig

cfg = TraceSageConfig(
    host="127.0.0.1",
    port=7842,
    sample_rate=0.1,           # capture 10% of runs in production
    per_run_event_cap=10_000,  # circuit breaker per run
)
tracer = await TraceSage.create(config=cfg)
```

See [configuration.md](configuration.md) for the full list.

## Multiple invocations

The tracer is created once at app startup. Reuse `tracer.handler` across every
invocation:

```python
tracer = await TraceSage.create()

for input_payload in incoming_requests:
    result = await graph.ainvoke(
        input_payload,
        config={"callbacks": [tracer.handler], "tags": ["api-v2"]},
    )
```

Each invocation creates a separate run in the dashboard. Tags propagate.

## Stopping cleanly

`TraceSage` registers an `atexit` cleanup, but for explicit shutdown:

```python
await tracer.stop()  # drains queue, closes DB, stops server
```

## Viewing data later

The UI is also available as a CLI viewer that does not ingest:

```bash
tracesage serve --data-dir ~/.tracesage
```

Useful for inspecting traces from a previous session, or running the viewer on
a different machine pointed at a synced data directory.

## Next steps

- [development.md](development.md) — trace links, sync/notebook setup, terminal debugging (`show`/`watch`/`diff`), the `tracesage_capture` pytest fixture
- [configuration.md](configuration.md) — every knob (including the `TRACESAGE_ENABLED` kill switch)
- [production.md](production.md) — sampling, auth, retention, disabling, deployment patterns
- [cli.md](cli.md) — `serve`, `demo`, `show`, `watch`, `diff`, `view`, `export`, `stats`, `gc`
- [mcp.md](mcp.md) — attributing tools to their MCP server
- [comparison.md](comparison.md) — when to use tracesage vs LangSmith / Phoenix / LangFuse

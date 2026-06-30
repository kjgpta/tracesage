# Quickstart

## See it in 30 seconds (no API key needed)

```bash
pip install "tracesage[langchain]"
tracesage demo        # seeds a sample trace and opens the live UI
```

That's it. `tracesage demo` opens a real trace in your browser with no API key, no
config, and no agent code to write. Click around the topology graph, open a step's
payload, inspect token usage — then come back here to wire it into your own agent.

## Add it to your own agent

```python
result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tracer.handler]},   # ← the only line you add
)
# tracesage prints: 🔍 http://localhost:7842/ui  — open it to watch live
```

Full setup (one-time, at the start of your `async def main()`):

```python
from tracesage import TraceSage
tracer = await TraceSage.create()   # starts the UI server, returns the handler
```

## Install details

(Quote the extra on zsh — the default macOS shell — or the brackets glob and you get
`no matches found`. Double quotes work: `pip install "tracesage[langchain]"`.)

`tracesage` requires Python 3.11+ and pulls `langchain-core`. If your app uses
**LangGraph**, also `pip install langgraph` (tracesage doesn't pull it).
tracesage is **provider-agnostic** — it traces the LangChain callback stream, so
OpenAI, Anthropic, and local models are all captured automatically.

### Using a real provider

The examples use a fake model so they need no key. For your own app:

```bash
# OpenAI
pip install langchain-openai      && export OPENAI_API_KEY=...
# or Anthropic
pip install langchain-anthropic   && export ANTHROPIC_API_KEY=...
```

## A complete runnable example (no API key)

Copy this into `quickstart_demo.py` and run it with `python quickstart_demo.py`.
It uses `FakeListChatModel`, so it needs **no API key or provider** — it traces a
real LangChain chain end-to-end and opens a live trace:

```python
import asyncio
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.prompts import ChatPromptTemplate
from tracesage import TraceSage

async def main():
    # A trivial LangChain chain: prompt | model (no API key needed).
    model = FakeListChatModel(responses=["Paris is the capital of France."])
    chain = ChatPromptTemplate.from_template("What is the capital of {country}?") | model

    async with TraceSage.session(install=True) as tl:   # starts UI + global capture
        result = await chain.ainvoke({"country": "France"})
        await tl.flush()                                 # ensure events are persisted
        print("answer:", result.content)
        print("trace UI:", tl.ui_url)                    # open this
        input("Trace ready — open the printed link, then press Enter to exit.")

asyncio.run(main())
```

`install=True` captures every LangChain call globally, so you don't pass
`callbacks=`. The `input(...)` keeps the one-shot process (and its embedded UI)
alive while you look; the trace also persists for later `tracesage serve`.

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

Open the UI to see the trace live as it runs. tracesage prints the exact URL on
startup — open **that** link:

```
🔍 tracesage: http://127.0.0.1:7842/ui/#run=...
```

It's `http://localhost:7842/ui` by default, but if `7842` is busy (e.g. you're
running a second app) auto-port picks the next free one — `7843`, `7844`, … — so
the printed URL is the source of truth. `tracer.ui_url` exposes it in code.

!!! tip "One data dir per application"
    Traces persist to a `data_dir` (default `~/.tracesage`), and the run list,
    topology, and "Tools by source" are all scoped to that dir. If you run more
    than one app, give each its own `data_dir` so their graphs don't merge — and
    if a run "goes missing", it's almost always because the viewer points at a
    different dir than the writer. See
    [Isolating multiple applications](configuration.md#isolating-multiple-applications)
    and [Troubleshooting → "Where are my runs?"](troubleshooting.md#where-are-my-runs).

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
- [troubleshooting.md](troubleshooting.md) — "where are my runs?", install/port issues, FAQ
- [comparison.md](comparison.md) — when to use tracesage vs LangSmith / Phoenix / LangFuse

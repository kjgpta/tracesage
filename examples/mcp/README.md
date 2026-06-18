# Example 4 — MCP tools attributed by source

Shows tracesage distinguishing tools that come from **MCP servers** from tools
**hardcoded** in your workflow, in **three scenarios**. Each tool node in the
topology graph gets a coloured **ring + server-name chip** for its MCP server, a
matching dynamic **legend**, and a polished, collapsible **"Tools by source"**
panel — all using one consistent colour per server.

Two local stdio MCP servers back the MCP scenarios (no network, no API key):

- **`weather`** (`weather_server.py`) — 4 tools: `get_weather`, `get_forecast`, `severe_alerts`, and `air_quality`
  (the graphs deliberately do **not** call `air_quality` — it shows that the topology
  still lists a server's tools even when uninvoked: open `mcp:weather` to see all 4)
- **`math`** (`math_server.py`) — 2 tools: `add`, `multiply`

In the topology each MCP server is its own node connected to the tools it provides;
agents that call an MCP tool are linked to that server (agent → mcp edge). Click an
MCP server to see all its tools (called or not); click an agent to see the MCP servers
and in-code tools it uses.

A `FakeListChatModel` drives a planner node; graph nodes invoke the tools directly
so every call produces a real tool event.

## The scenarios

| Script | What it shows |
|---|---|
| `mcp_only.py`              | **Only MCP tools** — one agent per server (weather provides 4 tools, 3 called; math 2), no Local group |
| `local_only.py`            | **Only hardcoded tools** — a single Local group, no MCP nodes/legend |
| `main.py`                  | **Mixed** — multiple agents + 2 MCP servers + 2 hardcoded local tools |
| `single_agent_multi_mcp.py`| **One agent → multiple MCP servers** — a single `researcher` agent calls tools from both weather and math |

Each MCP server appears as its own **`mcp` node** in the topology, with edges to the
tools it provides; tools carry a dashed ring in their server's colour. Selecting a
run shows the same provenance in the **run-trace** view (the MCP servers that backed
the tools the agents called).

## Run

```bash
pip install 'tracesage[mcp]'

python examples/mcp/mcp_only.py               # then open http://localhost:7842/ui
python examples/mcp/local_only.py
python examples/mcp/main.py                   # mixed: multiple agents
python examples/mcp/single_agent_multi_mcp.py # one agent, multiple servers
```

Add `--check` to any of them to run once, print the inventory, and exit (no server wait):

```bash
python examples/mcp/main.py --check
```

## Exporting to OpenTelemetry (optional)

Besides the local UI, these traces can be shipped to any OTLP backend as OpenTelemetry
spans. Start a listener on `:4318`, then enable export — `main.py` takes an `--otlp`
flag, and every example also honours the `TRACESAGE_OTLP_ENDPOINT` env var:

```bash
pip install "tracesage[otel]"

# 1. a backend that receives + shows spans (pick one):
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest   # UI: :16686
#   or, no Docker (macOS):  brew install ymtdzzz/tap/otel-tui && otel-tui

# 2. run with export on:
python examples/mcp/main.py --otlp http://localhost:4318
#   or:  TRACESAGE_OTLP_ENDPOINT=http://localhost:4318 python examples/mcp/mcp_only.py
```

You'll see the run as a span tree (`run LangGraph` → `chain weather_agent` →
`tool get_weather` with `tracesage.mcp_server=weather`, …) in the backend's UI — the
exported spans show up **there**, not in tracesage's own UI. Best-effort: with no
listener on `:4318`, spans go nowhere but tracing and the local UI still work. See
[docs/configuration.md](../../docs/configuration.md#opentelemetry-export).

## What you'll see

In the UI's **"Tools by source"** panel (top-right of the graph pane):

```
MCP: weather  -> 4 tools   air_quality, get_forecast, get_weather, severe_alerts
MCP: math     -> 2 tools   add, multiply
Local         -> 2 tools   format_report, uppercase
```

Click any `tool:` node in the topology graph — the drawer shows its **Source**
(`MCP weather` / `MCP math` / `local`). The same data is available at
`GET /api/tools`.

## How attribution works

`register_mcp_client(tracer, client)` loads each server's tools and records a
`tool_name -> server` mapping in the tracer. When a tool fires, the callback
handler tags the event with its MCP server, which is persisted on the event and
surfaced in the topology and the `/api/tools` inventory. Tools with no mapping
(your hardcoded `@tool`s) fall into the **local** bucket.

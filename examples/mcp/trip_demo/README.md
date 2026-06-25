# Trip Planner — MCP demo

A single ReAct agent that plans a weekend trip from NYC to Tokyo by querying **three MCP servers** (flights, weather, hotels — 7 tools each) plus one local formatting tool. tracesage attributes every tool call to its source server — visible in the topology graph and "Tools by source" panel.

Two scripts, **identical** agent / query / MCP client / LLM — the only difference is the minimal tracesage wiring in `demo.py`:

| Script | What it shows |
|---|---|
| **`before.py`** | The agent with **no observability** — just the final travel brief printed to the terminal. Run this first to feel the pain: you can't see which tools fired, what they returned, how many LLM calls/tokens it took, or where time went. |
| **`demo.py`** | The **same** run with tracesage added — every tool call attributed to its server, full request/response payloads, token usage, and a live timeline in the browser. |

> **Demo tip:** run `before.py` first and ask "okay — which of the 22 tools actually fired, and what did each return?" Nobody can answer from the terminal. Then run `demo.py` and answer all of it from the UI.

## What tracesage shows

| UI surface | What you see |
|---|---|
| **Topology graph** | 1 agent node → 3 coloured MCP server nodes → 21 MCP tools (+ 1 local) |
| **Tools by source panel** | `flights (7) · weather (7) · hotels (7) · Local (1)` |
| **MCP server drawer** | Click any server node → full tool list + call history for that server |
| **Tool event drawer** | Click any tool call → "Source: MCP flights" (or weather / hotels / Local) |
| **LLM node drawer** | Token usage (in / out, total across calls) and per-call latency |

## Demo arc

**Step 0 — Show the "before"**
Run `before.py`. The terminal sits silent, then prints a travel brief. That's all you get — no tool log, no token counts, no timeline. List the questions you *can't* answer.

**Step 1 — Minimal setup**
Point at `demo.py`: `TraceSage.create()` + `register_mcp_client()`, then a callback on `ainvoke`. That's the entire tracesage integration on top of the same standard LangGraph agent.

**Step 2 — Watch the agent work**
Run `demo.py`. The terminal shows the LLM's tool-call decisions in real time. Open the UI side-by-side and watch events stream in as the agent queries each server.

**Step 3 — Explore the attribution**
Open the Topology tab. Three differently-coloured MCP server nodes fan out from the agent. Click the `hotels` node — the drawer shows all seven of its tools (including ones not called this run), since tracesage persists the full server inventory at startup.

## Prerequisites

```bash
pip install 'tracesage[mcp]'
export ANTHROPIC_API_KEY=...      # default provider
```

## Run

```bash
# 1. The "before" — no observability, final answer only
python examples/mcp/trip_demo/before.py

# 2. The "after" — same agent + tracesage; keeps the UI server alive for exploration
python examples/mcp/trip_demo/demo.py

# Auto-open browser once the trace is written
python examples/mcp/trip_demo/demo.py --open

# Switch to OpenAI
export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...
python examples/mcp/trip_demo/demo.py

# Smoke test — run agent then exit (needs API key)
python examples/mcp/trip_demo/demo.py --check
```

Then open **http://localhost:7842/ui**.

## File layout

```
trip_demo/
├── before.py            # the same agent with NO tracesage — final answer only
├── demo.py              # entry point — same agent + minimal tracesage wiring
├── flights_server.py    # MCP server: search_flights, get_baggage_policy, +5 more
├── weather_server.py    # MCP server: get_weather, get_7day_forecast, +5 more
└── hotels_server.py     # MCP server: search_hotels, get_hotel_details, +5 more
```

`diff before.py demo.py` shows the exact tracesage integration — a couple of imports,
the setup calls, and a `callbacks=[tracer.handler]` kwarg on `ainvoke`. A minimal,
mechanical change with no rewrite of the agent.

Data in all three servers is hardcoded but formatted to look realistic — no external API calls required.

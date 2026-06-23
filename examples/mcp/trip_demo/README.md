# Trip Planner — MCP demo

A single ReAct agent that plans a weekend trip from NYC to Tokyo by querying **three MCP servers** (flights, weather, hotels) plus one local formatting tool. tracesage attributes every tool call to its source server — visible in the topology graph and "Tools by source" panel.

## What tracesage shows

| UI surface | What you see |
|---|---|
| **Topology graph** | 1 agent node → 3 coloured MCP server nodes → 6 tools |
| **Tools by source panel** | `flights (2) · weather (2) · hotels (2) · Local (1)` |
| **MCP server drawer** | Click any server node → full tool list + call history for that server |
| **Tool event drawer** | Click any tool call → "Source: MCP flights" (or weather / hotels / Local) |

## Demo arc (3 steps)

**Step 1 — Two lines of setup**
Point at `demo.py`: `TraceSage.create()` + `register_mcp_client()`. That's the entire tracesage integration on top of a standard LangGraph agent.

**Step 2 — Watch the agent work**
Run the command below. The terminal shows the LLM's tool-call decisions in real time. Open the UI side-by-side and watch events stream in as the agent queries each server.

**Step 3 — Explore the attribution**
Open the Topology tab. Three differently-coloured MCP server nodes fan out from the agent. Click the `hotels` node — the drawer shows both its tools, including `get_hotel_details`, even if only one was invoked (tracesage persists the full server inventory at startup).

## Prerequisites

```bash
pip install 'tracesage[mcp]'
export ANTHROPIC_API_KEY=...      # default provider
```

## Run

```bash
# Basic run — keeps the UI server alive for exploration
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
├── demo.py              # entry point — agent + tracesage wiring
├── flights_server.py    # MCP server: search_flights, get_baggage_policy
├── weather_server.py    # MCP server: get_weather, get_7day_forecast
└── hotels_server.py     # MCP server: search_hotels, get_hotel_details
```

Data in all three servers is hardcoded but formatted to look realistic — no external API calls required.

# 20 — Multi-MCP Travel Planner

**Domain:** travel · **Base:** LangGraph · **Pattern:** single agent over many MCPs

One `create_react_agent` plans a trip by calling tools from TWO local FastMCP stdio servers
at once — `flights` (`search_flights`, `baggage_policy`) and `weather` (`get_weather`,
`get_forecast`) — loaded together through langchain-mcp-adapters'
`MultiServerMCPClient`. The agent interleaves calls across both servers to answer a single
"London → Tokyo" travel request.

## Run

```bash
pip install -r ../requirements.txt
pip install 'tracesage[mcp]' mcp langchain-mcp-adapters   # MCP extras
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

The two `*_server.py` files are tiny MCP servers started as subprocesses by the client —
you do not run them yourself.

## The integration

```bash
diff before.py after.py
```

The only difference is the `from tracesage import TraceSage` /
`from tracesage.adapters.mcp import register_mcp_client` imports and wrapping the run in
`async with TraceSage.session(install=True)`. `register_mcp_client(tl, client)` loads the
tools AND records which server each came from; `install=True` registers the global
LangChain handler, so there is still no `callbacks=` wiring on the agent.

## What the trace shows

- **A single agent spanning multiple MCP servers** — the multi-MCP visualization: one
  `researcher`-style agent node fans out to tools color-coded by their source server.
- **Tool-source attribution**: `search_flights` / `baggage_policy` attributed to the
  `flights` server, `get_weather` / `get_forecast` to the `weather` server.
- The agent's **ReAct loop** — interleaved LLM reasoning steps and tool calls until it has
  enough to summarize, with per-step latency and token usage.
- Tools each server exposes even if the agent never calls them, so you can see the full
  cross-server tool inventory in one topology view.

# Support Assistant — minimal MCP demo

The simplest possible "agent over MCP" story: a customer-support assistant that
answers **"Where is my order, and what's your delivery policy?"** by calling two
small MCP servers, then drafting a reply.

Deliberately tiny — **2 servers × 2 tools** — so the topology stays clean and the
whole flow is easy to follow on screen. (See [`../trip_demo/`](../trip_demo/) for a
larger 3-server / 21-tool version.)

## Two runs: one succeeds, one fails — that's the point

Both scripts answer **two** customer tickets in a row, under one tracer:

| Ticket | What happens |
|---|---|
| **A1043** | Order has shipped → the run **succeeds**: looks up the order, its shipping status, the delivery policy, and drafts a reply. |
| **A1044** | This order's record lives on a DB shard that's **down** → `look_up_order` errors, the run **fails**. |

This is where observability earns its keep. When A1044 breaks:

- **`before.py`** gives you a bare `_MCPToolExecutionError` buried in a wall of debug
  logs — good luck finding *which* tool, with *what* input, returned *what*.
- **`after.py`** shows it in the UI: A1043 is a green **completed** run, A1044 is a red
  **failed** run with an error node sitting on the exact `look_up_order` call —
  the order id (`A1044`) and the `orders-02 shard unavailable` message, right there.

Two scripts, **identical** agent / queries / MCP client / LLM — the only difference is
the minimal tracesage wiring in `after.py`:

| Script | What it shows |
|---|---|
| **`before.py`** | The agent with **no observability** — just the final drafted reply (and a `✗ run failed` for A1044), plus a wall of hand-rolled debug logs that still can't tell you which server did what, or where A1044 broke. |
| **`after.py`** | The **same** runs with tracesage added — every tool call attributed to its server (`orders` vs `kb`), full request/response payloads, token usage, a live timeline, and the failed run pinpointed to a single tool call. |

## What tracesage shows

| UI surface | What you see |
|---|---|
| **Run list** | two runs — **A1043 completed** (green), **A1044 failed** (red) |
| **Topology graph** | 1 agent → 2 coloured MCP server nodes (`orders`, `kb`) → 4 tools + 1 local |
| **Tools by source** | `orders (2) · kb (2) · Local (1)` |
| **Timeline (A1043)** | `look_up_order → get_shipping_status → get_policy → draft_reply`, each with full payloads |
| **Failed run (A1044)** | a red error node on `look_up_order` with the exact `order_id` input and the shard error |
| **LLM node** | token usage (in / out, total across calls) and latency |

> The failure is deliberate and deterministic: `after.py` loads the MCP tools with
> `register_mcp_client(..., handle_tool_errors=False)`, so a tool that errors
> server-side **raises** and fails the run (instead of feeding the error back to the
> model to paper over). That's the knob that turns a silent bad answer into a visible
> red error node.

## The servers

- **`orders`** — `look_up_order`, `get_shipping_status`
  (`look_up_order` raises for **A1044** — its shard `orders-02` is "down" — which is
  what makes the second run fail)
- **`kb`** (knowledge base) — `search_help_center`, `get_policy`
- **Local** — `draft_reply` (your own code, not an MCP server)

All data is hardcoded but realistic — no external APIs, just an LLM key.

## Run

```bash
pip install 'tracesage[mcp]'
export ANTHROPIC_API_KEY=...      # or OPENAI_API_KEY (set LLM_PROVIDER=openai)

# 1. The "before" — no observability, final answer only
python examples/mcp/support_demo/before.py

# 2. The "after" — same agent + tracesage; keeps the UI alive for exploration
python examples/mcp/support_demo/after.py --open
```

`diff before.py after.py` shows the exact tracesage integration — two imports, two
setup lines, and one `callbacks=[tracer.handler]` kwarg.

## File layout

```
support_demo/
├── before.py          # the same agent with NO tracesage — final answer only
├── after.py            # same agent + minimal tracesage wiring
├── orders_server.py   # MCP server: look_up_order, get_shipping_status
└── kb_server.py       # MCP server: search_help_center, get_policy
```

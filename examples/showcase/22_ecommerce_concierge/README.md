# 22 — E-commerce Shopping Concierge

**Domain:** e-commerce · **Base:** LangChain · **Pattern:** action tools

A tool-calling `AgentExecutor` that helps a shopper find and buy products. It owns three
local action tools: `search_catalog` (a canned product list), `add_to_cart` (mutates an
in-memory cart), and `view_cart`. Given a single request, the agent decides which tools to
call and in what order — searching, mutating cart state, then confirming the total.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `import tracelens` and wrapping the run in `with tracelens.trace():`
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global
handler captures the agent and every tool call automatically.

## What the trace shows

- The **agent reasoning loop**: each LLM step deciding which tool to call next, nested
  under the `AgentExecutor`.
- **Side-effecting action tools** (cart mutations) visible as distinct tool calls — you can
  see each `add_to_cart` invocation, its `sku`/`quantity` arguments, and the cart state it
  returned, in order.
- The interleaving of `search_catalog` → `add_to_cart` → `view_cart`, so you can confirm
  the agent searched before mutating and verified the cart before finishing.
- Per-step **latency and token usage**, with full tool inputs/outputs in the drawer.

This is a good look at how tracelens makes opaque agent tool use — including state changes
to external systems — auditable step by step.

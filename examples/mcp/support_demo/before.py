"""Support Assistant MCP demo — WITHOUT tracesage (the "before" picture).

A single ReAct agent answers a customer's question using two MCP servers
(orders, kb) plus one local tool to draft the reply. Two tools per server —
deliberately small, so the whole flow is easy to follow.

That's all you get: the final drafted reply. This is the everyday reality of
running an agent over MCP servers with no observability:

  • Which tools actually fired? Did it check the order AND the policy, or guess?
  • What did each tool RETURN? If the reply is wrong, was it a bad order lookup
    or the LLM ignoring a correct policy?
  • How many LLM round-trips? How many tokens? Where did the time go?

Run `after.py` next to see the same agent with tracesage added — a minimal
change — answering every question above in a live local UI.

Run it (same setup as after.py):
    pip install 'tracesage[mcp]'
    export ANTHROPIC_API_KEY=...              # default: Anthropic claude-haiku-4-5
    python examples/mcp/support_demo/before.py

    (A .env file in the repo root is loaded automatically, so ANTHROPIC_API_KEY /
    OPENAI_API_KEY can live there instead of being exported.)

Switch to OpenAI:
    export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...
    python examples/mcp/support_demo/before.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:
    sys.exit("MCP support missing. Install: pip install 'tracesage[mcp]'")

from langgraph.prebuilt import ToolNode, create_react_agent

HERE = Path(__file__).resolve().parent

# ── Developer-rolled "observability" ─────────────────────────────────────────
# Without a tracing tool, this is what you end up doing: sprinkle ad-hoc logging
# and hope it tells you enough. It's noisy, only shows what THIS developer thought
# to log, and still can't answer the real questions (which server? how many tokens?).
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("support_agent")
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langgraph").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.INFO)

# Identical queries to after.py — the only difference between the two files is that
# after.py adds tracesage. Everything else is byte-for-byte the same.
# Two tickets: A1043 succeeds, A1044 fails (its shipping lookup errors). Without
# tracesage, when the second one breaks you get a stack trace buried in the logs
# and a pile of guesswork about which tool, which input, and what it returned.
QUERIES = [
    ("A1043 — happy path",
     "A customer wrote in: \"Where is my order A1043, and what's your delivery policy?\" "
     "Look up the order and its shipping status, check the delivery policy, "
     "then call draft_reply with a friendly, accurate response."),
    ("A1044 — failure path",
     "A customer wrote in: \"Where is my order A1044?\" "
     "You MUST start by calling look_up_order with order_id A1044 to fetch the "
     "record — do not answer from memory. Then get its shipping status and "
     "call draft_reply."),
]


# ── Local tool ────────────────────────────────────────────────────────────────

@tool
def draft_reply(message: str) -> str:
    """Format the final reply to the customer. Call this last with your complete message."""
    border = "=" * 54
    return f"\n{border}\n  DRAFT REPLY TO CUSTOMER\n{border}\n{message}\n{border}"


# ── Setup helpers (identical to after.py) ───────────────────────────────────────

_PROVIDER_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def make_llm() -> Runnable:
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    model = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
    key_var = _PROVIDER_KEY.get(provider, f"{provider.upper()}_API_KEY")
    if not os.environ.get(key_var):
        sys.exit(
            f"\nNo LLM API key found. This demo defaults to '{provider}' and needs ${key_var}.\n\n"
            f"    export {key_var}=...\n"
            "    python examples/mcp/support_demo/before.py\n\n"
            "(Or add it to a .env file in the repo root — this script loads .env automatically.)\n"
            "Use a different provider: export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...\n"
        )
    return init_chat_model(model, model_provider=provider, temperature=0.0)


def make_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "orders": {
                "command": sys.executable,
                "args": [str(HERE / "orders_server.py")],
                "transport": "stdio",
            },
            "kb": {
                "command": sys.executable,
                "args": [str(HERE / "kb_server.py")],
                "transport": "stdio",
            },
        }
    )


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    log.debug("make_llm(): provider=%s model=%s", os.environ.get("LLM_PROVIDER", "anthropic"),
              os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001"))
    llm = make_llm()  # preflight: exits with setup steps if no LLM API key is set

    log.debug("connecting MCP client (orders, kb) over stdio…")
    client = make_mcp_client()
    # handle_tool_errors=False so a tool that errors server-side RAISES (the A1044
    # shard is down) — same as after.py, so both files fail identically. The only
    # difference is after.py can tell you WHERE; here you get a bare stack trace.
    from langchain_mcp_adapters.tools import load_mcp_tools
    tools = []
    for conn in client.connections.values():
        tools += await load_mcp_tools(None, connection=conn, handle_tool_errors=False)
    # We can log that N tools loaded — but NOT which server each came from.
    log.info("loaded %d MCP tools (which server is which? no idea from here): %s",
             len(tools), [getattr(t, "name", "?") for t in tools])
    all_tools = [*tools, draft_reply]
    agent = create_react_agent(llm, ToolNode(all_tools, handle_tool_errors=False))
    log.info("agent built with %d total tools; invoking…", len(all_tools))

    print("…running (hand-rolled DEBUG logs below — noisy, and still not enough)…\n")
    for label, query in QUERIES:
        print(f"\n=== {label} ===")
        print(f"Q: {query}\n")
        t0 = time.perf_counter()
        try:
            result = await agent.ainvoke(
                {"messages": [("user", query)]},
                config={"recursion_limit": 20},      # no callbacks=[tracer.handler]
            )
            elapsed = time.perf_counter() - t0
            log.info("agent.ainvoke() returned after %.2fs (total only — no per-step timing)", elapsed)
            print("\n" + result["messages"][-1].content)
        except Exception as e:
            elapsed = time.perf_counter() - t0
            # The run blew up somewhere inside. Which tool? Which input? What did
            # it return before failing? You'd have to scroll the DEBUG wall above
            # and reconstruct it by hand.
            log.error("agent.ainvoke() raised after %.2fs: %s", elapsed, e)
            print(f"✗ run failed: {type(e).__name__}: {e}")
            print("  (good luck finding WHICH tool, with WHAT input, in the logs above)")

    print(
        "\n"
        "↑ Two runs — one worked, one failed — and that's all you get without\n"
        "  tracesage, plus a wall of DEBUG logs. To find why A1044 broke you must\n"
        "  hand-scroll the logs: no per-server attribution, no error node, no\n"
        "  timeline, no token counts.\n"
        "  Run `python examples/mcp/support_demo/after.py` to see both runs traced —\n"
        "  the failed one shows a red error node on the exact tool call.\n"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")

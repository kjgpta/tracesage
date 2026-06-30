"""Support Assistant MCP demo — two MCP servers, one agent, full tracesage attribution.

A single ReAct agent answers a customer question using two MCP servers (orders, kb)
plus one local tool to draft the reply. Deliberately small — 2 tools per server —
so the topology stays clean and the whole flow is easy to follow.

tracesage records which tool call came from which server, visible in the topology
graph and "Tools by source" panel.

Run:
    pip install 'tracesage[mcp]'
    export ANTHROPIC_API_KEY=...              # default: Anthropic claude-haiku-4-5
    python examples/mcp/support_demo/after.py
    python examples/mcp/support_demo/after.py --open   # auto-open browser

    (A .env file in the repo root is loaded automatically, so ANTHROPIC_API_KEY /
    OPENAI_API_KEY can live there instead of being exported.)

Switch to OpenAI:
    export LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini OPENAI_API_KEY=...
    python examples/mcp/support_demo/after.py

Smoke test (run agent then exit — useful for CI):
    python examples/mcp/support_demo/after.py --check
"""
from __future__ import annotations

import asyncio
import os
import sys
import webbrowser
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

from tracesage import TraceSage, TraceSageConfig  # ← tracesage
from tracesage.adapters.mcp import register_mcp_client  # ← tracesage

HERE = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".tracesage" / "support-demo"

# Two customer tickets → two runs under the SAME tracer:
#   • A1043 has shipped  → the run SUCCEEDS (clean trace, draft reply)
#   • A1044's record is on a DB shard that's down → look_up_order errors, so the
#     run FAILS — and tracesage shows a red error node on look_up_order with the
#     exact input, instead of a silent bad answer.
# Identical to before.py — the only difference between the two files is that
# after.py adds tracesage.
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


# ── Local tool (shows up as "Local" in the Tools by source panel) ────────────

@tool
def draft_reply(message: str) -> str:
    """Format the final reply to the customer. Call this last with your complete message."""
    border = "=" * 54
    return f"\n{border}\n  DRAFT REPLY TO CUSTOMER\n{border}\n{message}\n{border}"


# ── Setup helpers ─────────────────────────────────────────────────────────────

_PROVIDER_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def make_llm() -> Runnable:
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    model = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
    key_var = _PROVIDER_KEY.get(provider, f"{provider.upper()}_API_KEY")
    if not os.environ.get(key_var):
        sys.exit(
            f"\nNo LLM API key found. This demo defaults to '{provider}' and needs ${key_var}.\n\n"
            f"    export {key_var}=...\n"
            "    python examples/mcp/support_demo/after.py\n\n"
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

async def main(*, check: bool = False, open_browser: bool = False) -> None:
    llm = make_llm()  # preflight: exits with setup steps if no LLM API key is set

    # ── tracesage: minimal wiring, that's it ─────────────────────────────────
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))
    # handle_tool_errors=False → a tool that errors server-side RAISES instead of
    # returning the error as content. That's what turns the A1044 ticket (whose
    # order shard is down) into a real failed run, shown in red in the UI — rather
    # than the model quietly papering over the error.
    mcp_tools = await register_mcp_client(
        tracer, make_mcp_client(), handle_tool_errors=False
    )
    # ─────────────────────────────────────────────────────────────────────────

    all_tools = [*mcp_tools, draft_reply]
    # Same flag for the local tool node, so every tool error (MCP or local)
    # propagates and fails the run instead of being fed back to the model.
    agent = create_react_agent(llm, ToolNode(all_tools, handle_tool_errors=False))

    # Run BOTH tickets under the same tracer → the UI run list shows one
    # completed run and one failed run. The failure is the point: tracesage
    # captures exactly where and why it broke.
    for label, query in QUERIES:
        print(f"\n=== {label} ===")
        print(f"Q: {query}\n")
        try:
            result = await agent.ainvoke(
                {"messages": [("user", query)]},
                config={"callbacks": [tracer.handler], "recursion_limit": 20},
            )
            print(result["messages"][-1].content)
        except Exception as e:
            # The agent run raised (a tool errored). tracesage has already
            # recorded the failed run + the error node; we just keep going.
            print(f"✗ run failed: {type(e).__name__}: {e}")
            print("  → open the UI: the failed run shows a red error node on the "
                  "exact tool call (look_up_order, order A1044) that broke.")

    await asyncio.sleep(0.5)  # let the worker batch drain to DB

    # Print the same breakdown the UI shows in "Tools by source"
    inv = await tracer.db.get_tool_inventory()
    print("\nTools attributed by tracesage:")
    for s in inv["sources"]:
        kind = "MCP  " if s["kind"] == "mcp" else "Local"
        names = [t["name"] for t in s["tools"]]
        print(f"  {kind}  {s['source']:<12} → {s['tool_count']} tools   {names}")

    if check:
        await tracer.stop()
        return

    # tracesage defaults to :7842 and auto-picks the next free port if it's busy —
    # print the URL it actually bound.
    url = tracer.ui_url or "http://localhost:7842/ui"
    print(f"\ntracesage UI → {url}")
    print("  Run list       — TWO runs: A1043 completed (green), A1044 failed (red)")
    print("  Failed run     — open it: a red error node on look_up_order shows the")
    print("                   'orders-02 shard unavailable' error + the exact order_id")
    print("  Topology tab   — 1 agent node fanning out to 2 coloured MCP server nodes")
    print("  Tools panel    — orders(2)  kb(2)  Local(1)")

    if open_browser:
        webbrowser.open(url)

    print("\nCtrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(
            main(
                check="--check" in sys.argv,
                open_browser="--open" in sys.argv or "-o" in sys.argv,
            )
        )
    except KeyboardInterrupt:
        print("\nstopped.")

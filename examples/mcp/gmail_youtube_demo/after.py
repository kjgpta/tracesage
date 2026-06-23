"""Gmail + YouTube research agent — with tracesage observability.

Identical to before.py except for the lines marked ← tracesage.
Run it, then open http://localhost:7842/ui:

  Topology tab   — agent node → gmail server node + youtube server node
  Timeline       — every tool call with full request/response payload
  Tools panel    — gmail tools and youtube tools labelled by source server
  Token counts   — per-LLM-call, visible on each timeline card

Prerequisites:
    pip install 'tracesage[mcp]' mcp-google-gmail mcp-youtube-transcript langchain-anthropic langchain-openai
    uv tool install mcp-google-gmail          # installs the auth CLI
    mcp-google-gmail auth                     # one-time browser OAuth

Run (set whichever key you have — Anthropic is the default):
    export ANTHROPIC_API_KEY=...              # default
    export OPENROUTER_API_KEY=...             # or use OpenRouter
    python examples/mcp/gmail_youtube_demo/after.py
    python examples/mcp/gmail_youtube_demo/after.py --open   # auto-open browser
"""
from __future__ import annotations

import asyncio
import os
import sys
import sysconfig
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from langchain.chat_models import init_chat_model

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:
    sys.exit("MCP support missing. Install: pip install 'tracesage[mcp]'")

from langgraph.prebuilt import create_react_agent

from tracesage import TraceSage, TraceSageConfig              # ← tracesage (1)
from tracesage.adapters.mcp import register_mcp_client       # ← tracesage (2)

DATA_DIR = Path.home() / ".tracesage" / "gmail-youtube-demo"

FALLBACK_VIDEO = os.environ.get(
    "YOUTUBE_URL",
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
)

QUERY = (
    "Search my Gmail inbox for the 5 most recent unread emails and read each one. "
    "Look for any YouTube video URLs in the email bodies. "
    "If you find YouTube links, fetch the transcript of the most interesting one. "
    f"If there are no YouTube links in any email, fetch the transcript of this video instead: {FALLBACK_VIDEO} "
    "Summarise the key points from the video. "
    "Only mention emails that are directly relevant to the video topic — ignore unrelated ones entirely."
)


def _script(name: str) -> str:
    """Find an installed console script using sysconfig scripts dir."""
    return str(Path(sysconfig.get_path("scripts")) / name)


def make_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "gmail": {
                "command": _script("mcp-google-gmail"),
                "args": [],
                "transport": "stdio",
                "env": {**os.environ},
            },
            "youtube": {
                "command": _script("mcp-youtube-transcript"),
                "args": [],
                "transport": "stdio",
                "env": {**os.environ},
            },
        }
    )


def make_llm():
    if "OPENROUTER_API_KEY" in os.environ:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=os.environ.get("LLM_MODEL", "anthropic/claude-haiku-4-5"),
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            temperature=0,
        )
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    model = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
    return init_chat_model(model, model_provider=provider, temperature=0)


async def main(*, open_browser: bool = False) -> None:
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))   # ← tracesage (3)
    mcp_tools = await register_mcp_client(tracer, make_mcp_client())      # ← tracesage (4)

    llm = make_llm()
    agent = create_react_agent(llm, mcp_tools)

    url = "http://localhost:7842/ui"
    print(f"\ntracesage UI → {url}  (open now to watch live)\n")
    if open_browser:
        webbrowser.open(url)
        await asyncio.sleep(2)  # let browser load and establish WebSocket before agent fires

    print(f"Q: {QUERY}\n")
    result = await agent.ainvoke(
        {"messages": [("user", QUERY)]},
        config={"callbacks": [tracer.handler], "recursion_limit": 25},    # ← tracesage (5)
    )
    print(result["messages"][-1].content)

    await asyncio.sleep(0.5)  # let worker batch drain to DB

    print("\nCtrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(
            main(open_browser="--open" in sys.argv or "-o" in sys.argv)
        )
    except KeyboardInterrupt:
        print("\nstopped.")

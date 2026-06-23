"""Gmail + YouTube research agent — no observability.

A ReAct agent reads your real Gmail inbox, extracts YouTube links from
emails, fetches their transcripts, and summarises everything. That's all
you get: the final answer printed to the terminal. No visibility into which
tools fired, what they returned, how many LLM calls were made, or where
time was spent.

Compare with after.py — the diff is the entire tracesage pitch.

Prerequisites:
    pip install 'tracesage[mcp]' mcp-google-gmail mcp-youtube-transcript langchain-anthropic langchain-openai
    uv tool install mcp-google-gmail          # installs the auth CLI
    mcp-google-gmail auth                     # one-time browser OAuth

Run (set whichever key you have — Anthropic is the default):
    export ANTHROPIC_API_KEY=...              # default
    export OPENROUTER_API_KEY=...             # or use OpenRouter
    python examples/mcp/gmail_youtube_demo/before.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from langchain.chat_models import init_chat_model

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:
    sys.exit("MCP support missing. Install: pip install 'tracesage[mcp]'")

from langgraph.prebuilt import create_react_agent

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


async def main() -> None:
    llm = make_llm()

    client = make_mcp_client()
    tools = await client.get_tools()
    agent = create_react_agent(llm, tools)

    print(f"Q: {QUERY}\n")
    result = await agent.ainvoke(
        {"messages": [("user", QUERY)]},
        config={"recursion_limit": 25},
    )
    print(result["messages"][-1].content)
    # That's it. Final answer only — no trace, no topology, no tool details.


if __name__ == "__main__":
    asyncio.run(main())

"""Gmail + YouTube research agent — no observability.

A ReAct agent reads your real Gmail inbox, extracts YouTube links from
emails, fetches their transcripts, and summarises everything. That's all
you get: the final answer printed to the terminal. No visibility into which
tools fired, what they returned, how many LLM calls were made, or where
time was spent.

Compare with after.py — the diff is the entire tracesage pitch.

Prerequisites:
    pip install 'tracesage[mcp]' mcp-google-gmail mcp-youtube-transcript langchain-anthropic langchain-openai

    YouTube works with no auth. Gmail is OPTIONAL — the mcp-google-gmail server
    needs Google Application Default Credentials (it calls google.auth.default()):
        gcloud auth application-default login           # easiest, needs gcloud
        # …or point GOOGLE_APPLICATION_CREDENTIALS at an OAuth/service-account JSON
    Without Gmail creds the Gmail server just fails to load and the agent runs with
    YouTube only. See the server's own docs for the exact GCP / Gmail-API setup.

Run (set whichever key you have — Anthropic is the default):
    export ANTHROPIC_API_KEY=...              # default
    export OPENROUTER_API_KEY=...             # or use OpenRouter
    python examples/mcp/gmail_youtube_demo/before.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import sysconfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

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


# External MCP servers this demo drives — each a PyPI package whose console
# script we launch over stdio (falling back to `uvx <pkg>` if uv is installed).
MCP_SERVERS = {
    "gmail":   {"script": "mcp-google-gmail",       "pkg": "mcp-google-gmail"},
    "youtube": {"script": "mcp-youtube-transcript", "pkg": "mcp-youtube-transcript"},
}


def _resolve_server_cmd(script: str, pkg: str) -> list[str] | None:
    """argv to launch an MCP server: prefer an installed console script (this
    venv, then PATH); fall back to `uvx <pkg>` if uv is available; else None."""
    venv_script = Path(sysconfig.get_path("scripts")) / script
    if venv_script.exists():
        return [str(venv_script)]
    on_path = shutil.which(script)
    if on_path:
        return [on_path]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, pkg]
    return None


def make_mcp_client() -> MultiServerMCPClient:
    """Build the client, exiting with clear setup instructions if a server isn't
    installed — rather than silently running a tool-less agent."""
    servers, missing = {}, []
    for name, spec in MCP_SERVERS.items():
        cmd = _resolve_server_cmd(spec["script"], spec["pkg"])
        if cmd is None:
            missing.append(spec["pkg"])
            continue
        servers[name] = {
            "command": cmd[0],
            "args": cmd[1:],
            "transport": "stdio",
            "env": {**os.environ},
        }
    if missing:
        sys.exit(
            f"\nThis demo needs external MCP servers that aren't installed: {', '.join(missing)}\n\n"
            "Install them into this environment, then re-run:\n"
            f"    pip install {' '.join(missing)}\n\n"
            "(Or install uv — https://astral.sh/uv — and they'll run via uvx automatically.)\n"
            "Gmail also needs Google credentials (gcloud auth application-default login).\n"
            "Full setup: examples/mcp/gmail_youtube_demo/README.md\n"
        )
    return MultiServerMCPClient(servers)


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

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
import shutil
import sys
import sysconfig
import webbrowser
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

from tracesage import TraceSage, TraceSageConfig  # ← tracesage (1)
from tracesage.adapters.mcp import register_mcp_client  # ← tracesage (2)

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
            f"    pip install {' '.join(missing)}\n"
            "    mcp-google-gmail auth        # one-time Gmail OAuth (opens a browser)\n\n"
            "(Or install uv — https://astral.sh/uv — and they'll run via uvx automatically.)\n"
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


async def main(*, open_browser: bool = False) -> None:
    client = make_mcp_client()   # preflight: exits with setup steps if a server is missing
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))   # ← tracesage (3)
    mcp_tools = await register_mcp_client(tracer, client)                 # ← tracesage (4)

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

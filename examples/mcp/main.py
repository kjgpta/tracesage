"""MCP tools + hardcoded tools, attributed by source.

Loads tools from TWO local MCP servers (weather: 4 tools, math: 2 tools) via
langchain-mcp-adapters, plus TWO hardcoded @tool functions, then runs a small
LangGraph. tracesage attributes every tool call to its MCP server (or "local"),
so the UI's "Tools by source" panel shows (all of a server's tools are listed,
even ones the graph never calls — weather's air_quality is uncalled on purpose):

    MCP: weather  -> 4 tools
    MCP: math     -> 2 tools
    Local         -> 2 tools

No API key needed (a FakeListChatModel drives the planner node; the graph nodes
invoke the tools directly so every tool fires real on_tool_start/end events).

Run:
    pip install 'tracesage[mcp]'
    python examples/mcp/main.py            # then open http://localhost:7842/ui
    python examples/mcp/main.py --check     # run once, print inventory, exit

    # Also export OpenTelemetry spans to a collector (needs `tracesage[otel]` and a
    # listener on :4318 — e.g. Jaeger or otel-tui; see docs/configuration.md):
    python examples/mcp/main.py --otlp http://localhost:4318
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.tools import tool  # noqa: E402
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:  # pragma: no cover
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracesage import TraceSage, TraceSageConfig  # noqa: E402
from tracesage.adapters.mcp import register_mcp_client  # noqa: E402

HERE = Path(__file__).resolve().parent
# Each example uses its OWN data dir, so separate applications never interfere:
# topology and "Tools by source" are computed per data dir (i.e. per application).
DATA_DIR = Path.home() / ".tracesage" / "mcp-mixed"


# ---- Two hardcoded ("local") tools — NOT from any MCP server ----------------- #


@tool
def uppercase(text: str) -> str:
    """Uppercase a string."""
    return text.upper()


@tool
def format_report(text: str) -> str:
    """Wrap text in a report banner."""
    return f"=== REPORT ===\n{text}"


LOCAL_TOOLS = {t.name: t for t in (uppercase, format_report)}


class State(TypedDict):
    topic: str
    notes: list[str]


async def main(check: bool = False, otlp: str | None = None) -> None:
    # Only set otlp_endpoint when --otlp is passed, so the TRACESAGE_OTLP_ENDPOINT
    # env var still works when the flag is omitted.
    cfg_kwargs: dict = {"data_dir": DATA_DIR, "project_name": "MCP demo (mixed)"}
    if otlp:
        cfg_kwargs["otlp_endpoint"] = otlp
    tracer = await TraceSage.create(TraceSageConfig(**cfg_kwargs))
    print(f"tracesage UI: {tracer.ui_url}")
    print(f"Data dir:     {DATA_DIR}")
    print(f"Inspect CLI:  tracesage runs -d {DATA_DIR}")
    if tracer._config.otlp_endpoint:
        print(f"OTel export:  -> {tracer._config.otlp_endpoint}  (install: pip install 'tracesage[otel]')")

    # Two local MCP servers over stdio (started as subprocesses by the client).
    client = MultiServerMCPClient(
        {
            "weather": {
                "command": sys.executable,
                "args": [str(HERE / "weather_server.py")],
                "transport": "stdio",
            },
            "math": {
                "command": sys.executable,
                "args": [str(HERE / "math_server.py")],
                "transport": "stdio",
            },
        }
    )
    # Load every server's tools AND record tool -> server provenance in the tracer.
    mcp_tools = await register_mcp_client(tracer, client)
    by_name = {t.name: t for t in mcp_tools}
    print(f"Loaded {len(mcp_tools)} MCP tools: {sorted(by_name)}")

    llm = FakeListChatModel(responses=["Planning the trip...", "Done."])

    async def planner(state: State, config) -> dict:
        await llm.ainvoke("plan: " + state["topic"], config=config)
        return {"notes": []}

    async def weather_agent(state: State, config) -> dict:
        notes = list(state["notes"])
        for name, args in (
            ("get_weather", {"city": "London"}),
            ("get_forecast", {"city": "London"}),
            ("severe_alerts", {"region": "UK"}),
        ):
            if name in by_name:
                notes.append(str(await by_name[name].ainvoke(args, config=config)))
        return {"notes": notes}

    async def math_agent(state: State, config) -> dict:
        notes = list(state["notes"])
        for name, args in (("add", {"a": 2, "b": 3}), ("multiply", {"a": 4, "b": 5})):
            if name in by_name:
                notes.append(str(await by_name[name].ainvoke(args, config=config)))
        return {"notes": notes}

    async def report_agent(state: State, config) -> dict:
        joined = " | ".join(state["notes"])
        up = await LOCAL_TOOLS["uppercase"].ainvoke({"text": joined}, config=config)
        rep = await LOCAL_TOOLS["format_report"].ainvoke({"text": up}, config=config)
        return {"notes": [str(rep)]}

    g = StateGraph(State)
    g.add_node("planner", planner)
    g.add_node("weather_agent", weather_agent)
    g.add_node("math_agent", math_agent)
    g.add_node("report_agent", report_agent)
    g.add_edge(START, "planner")
    g.add_edge("planner", "weather_agent")
    g.add_edge("weather_agent", "math_agent")
    g.add_edge("math_agent", "report_agent")
    g.add_edge("report_agent", END)
    graph = g.compile()

    await graph.ainvoke(
        {"topic": "trip planning", "notes": []},
        config={"callbacks": [tracer.handler], "tags": ["mcp-demo"]},
    )

    # Let the worker drain, then print what tracesage computed.
    await asyncio.sleep(0.5)
    inv = await tracer.db.get_tool_inventory()
    print("\nTools by source (as tracesage attributed them):")
    for s in inv["sources"]:
        kind = "MCP" if s["kind"] == "mcp" else "Local"
        names = [t["name"] for t in s["tools"]]
        print(f"  {kind:5s} {s['source']:8s} -> {s['tool_count']} tools  {names}")

    if check:
        await tracer.stop()
        return

    print(f"\nOpen {tracer.ui_url} and look at the 'Tools by source' panel.")
    print("Ctrl+C to stop.")
    await asyncio.Event().wait()


def _flag_value(flag: str) -> str | None:
    """Read `--flag value` from argv (None if absent)."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


if __name__ == "__main__":
    # --otlp <endpoint> turns on OpenTelemetry export (or set TRACESAGE_OTLP_ENDPOINT).
    try:
        asyncio.run(main(check="--check" in sys.argv, otlp=_flag_value("--otlp")))
    except KeyboardInterrupt:
        print("\nstopped.")

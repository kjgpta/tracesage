"""OpenAI Agents SDK traced by tracesage (needs OPENAI_API_KEY).

``instrument=["openai_agents"]`` switches on the OpenTelemetry instrumentation for the
Agents SDK (workflow / agent / handoff / tool spans) plus the OpenAI client
instrumentation for the ``chat`` spans underneath, and turns off export to OpenAI's own
trace dashboard. Everything lands in the local tracesage UI.

    pip install "tracesage[openai-agents]" openai-agents
    export OPENAI_API_KEY=...
    python examples/frameworks/openai_agents_sdk.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import tracesage
from tracesage import TraceSageConfig

DATA_DIR = Path.home() / ".tracesage" / "openai-agents-demo"


async def main() -> None:
    cfg = TraceSageConfig(data_dir=DATA_DIR, project_name="openai-agents-demo")
    # instrument() must run BEFORE the Agents SDK is imported/used.
    tl = tracesage.start(cfg, instrument=["openai_agents"])

    from agents import Agent, Runner, function_tool

    @function_tool
    def get_time(tz: str) -> str:
        """Current time in an IANA timezone."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz)).strftime("%H:%M")

    clock = Agent(
        name="Clock Agent",
        instructions="Answer time questions using the get_time tool.",
        tools=[get_time],
    )
    triage = Agent(
        name="Triage Agent",
        instructions="Hand off anything about time to the Clock Agent; otherwise answer.",
        handoffs=[clock],
    )

    try:
        result = await Runner.run(triage, "What time is it in Tokyo?")
        print("  ->", result.final_output)
        print(f"\nOpen {tl.ui_url}: workflow → Triage Agent → handoff → Clock Agent → get_time")
        print("Ctrl+C to stop.")
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        tl.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("tracesage stopped.")

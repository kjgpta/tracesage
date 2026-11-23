"""Pydantic AI agent traced by tracesage — runs with NO API key.

Pydantic AI emits OpenTelemetry spans natively (GenAI semantic conventions).
``tracesage.start(instrument=["pydantic_ai"])`` routes them into tracesage in-process:
no collector, nothing over the network.

The agent uses Pydantic AI's built-in ``TestModel`` so this works offline; swap it for
``"openai:gpt-4o"`` (and set OPENAI_API_KEY) to trace a real model.

    pip install "tracesage[pydantic-ai]" pydantic-ai-slim
    python examples/frameworks/pydantic_ai_agent.py
    # then open the printed URL
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel

import tracesage
from tracesage import TraceSageConfig

# Dedicated data dir so this app's runs stay isolated from other applications.
DATA_DIR = Path.home() / ".tracesage" / "pydantic-ai-demo"

agent = Agent(
    TestModel(),  # calls every tool once, then replies — deterministic, no network
    instructions="You are a weather assistant. Use the tools, then answer briefly.",
)


@agent.tool
def get_weather(ctx: RunContext[None], city: str) -> str:
    """Current weather for a city."""
    return f"{city}: sunny, 22°C"


@agent.tool
def get_forecast(ctx: RunContext[None], city: str, days: int = 3) -> str:
    """Short forecast for a city."""
    return f"{city}: {days} days of light cloud, 18-24°C"


def main() -> None:
    cfg = TraceSageConfig(data_dir=DATA_DIR, project_name="pydantic-ai-demo")
    tl = tracesage.start(cfg, instrument=["pydantic_ai"])
    try:
        for prompt in ("What's the weather in Paris?", "Forecast for Tokyo this week?"):
            result = agent.run_sync(prompt)
            print(f"  {prompt!r:<40s} -> {result.output!r}")

        print(f"\nLeaving the server up. Open {tl.ui_url} — each prompt is one run:")
        print("  invoke_agent → chat → execute_tool get_weather / get_forecast → chat")
        print("Ctrl+C to stop.")
        try:
            import time

            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    finally:
        tl.stop()
        print("tracesage stopped.")


if __name__ == "__main__":
    main()

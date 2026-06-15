"""21 — DevOps Incident Responder (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows the dense tool-call sequence the agent runs while
investigating the alert, the latency of each `query_logs` / `query_metrics` /
`get_recent_deploys` call, and the final investigate-to-diagnose hop to the runbook step.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from tracelens import TraceLens  # ← tracelens


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


@tool
def query_logs(service: str) -> str:
    """Return the most recent error log lines for a service."""
    return (
        f"[{service}] 12:04:01 ERROR upstream connect timeout (10s) to db-primary\n"
        f"[{service}] 12:04:02 ERROR connection pool exhausted (200/200)\n"
        f"[{service}] 12:03:58 WARN  p99 latency 8400ms"
    )


@tool
def query_metrics(service: str) -> str:
    """Return current key metrics (error rate, latency, saturation) for a service."""
    return (
        f"{service}: error_rate=14.2% (baseline 0.3%), "
        "p99_latency=8.4s (baseline 220ms), db_pool_saturation=100%, cpu=38%"
    )


@tool
def get_recent_deploys(service: str) -> str:
    """Return deploys for a service in the last 2 hours, newest first."""
    return (
        f"{service}: 12:01 deploy v482 (changed db pool max 200->? config) by @dana; "
        "10:30 deploy v481 (copy tweak) by @lee"
    )


SYSTEM = (
    "You are an on-call SRE. Investigate the alert using the tools, then reply with "
    "ONE runbook step prefixed 'RUNBOOK:'. Keep it under 3 sentences."
)


def build_agent() -> Runnable:
    llm = make_llm()
    return create_react_agent(
        llm,
        tools=[query_logs, query_metrics, get_recent_deploys],
        prompt=SYSTEM,
    )


async def main() -> None:
    agent = build_agent()
    alert = "PagerDuty: checkout-api error rate spiking, p99 latency over 8s"
    print(f"ALERT: {alert}\n")

    async with TraceLens.session(install=True) as tl:  # ← tracelens
        result = await agent.ainvoke(
            {"messages": [("user", alert)]},
            config={"recursion_limit": 12},
        )
        print(result["messages"][-1].content)
        await tl.flush()  # ← tracelens: ensure events persist
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(
                input, "\n🔍 Open the printed trace link, then press Enter to exit."
            )


if __name__ == "__main__":
    asyncio.run(main())

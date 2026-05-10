"""LangGraph nodes for the error-recovery pipeline.

Pattern:
    fetch_node → router (success | error)
                    ├ success → process_node → END
                    └ error   → fallback_node → process_node → END

The `fetch_node` calls `flaky_fetch.ainvoke(...)` inside try/except. If the
tool raises, it logs the error in state and the router takes the fallback
path. tracelens captures the underlying `tool_error` event regardless of
whether the caller catches the exception.
"""
from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import HumanMessage

from llm import get_llm
from tools import fallback_fetch, flaky_fetch, process_data


class RecoveryState(TypedDict, total=False):
    url: str
    payload: str
    used_fallback: bool
    error: str
    processed: str
    summary: str


# Module-level LLMs for the optional summary node (3 questions).
_summary_llm = get_llm(
    responses=[
        "Fetch succeeded; processed normally.",
        "Fetch failed; recovered via fallback.",
        "Fetch succeeded; processed normally.",
    ]
)


async def fetch_node(state: RecoveryState) -> dict:
    """Try the flaky fetch. Capture the error (if any) for the router."""
    try:
        payload = await flaky_fetch.ainvoke({"url": state["url"]})
        return {"payload": payload, "used_fallback": False, "error": ""}
    except Exception as e:  # noqa: BLE001 - intentionally broad for the demo
        # Recording the error in state lets the router branch on it. The
        # underlying tool_error callback already fired before this except.
        return {"payload": "", "used_fallback": True, "error": str(e)}


async def fallback_node(state: RecoveryState) -> dict:
    """Use the reliable fallback fetcher."""
    payload = await fallback_fetch.ainvoke({"url": state["url"]})
    return {"payload": payload, "used_fallback": True}


async def process_node(state: RecoveryState) -> dict:
    """Process whichever payload we ended up with."""
    processed = await process_data.ainvoke({"payload": state.get("payload", "")})
    return {"processed": processed}


async def summarize_node(state: RecoveryState) -> dict:
    """Optional summary — surfaces in the run's final state."""
    msg = await _summary_llm.ainvoke(
        [
            HumanMessage(
                content=f"Summarize: url={state['url']} fallback={state.get('used_fallback')}"
            )
        ]
    )
    return {"summary": msg.content}

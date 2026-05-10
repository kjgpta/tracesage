"""LangGraph wiring for the code review pipeline with a retry loop.

Layout:
    parse → analyze → comment → quality_check → router
                                                   ├─ retry → comment
                                                   └─ ok    → format → END

Retries cycle through the `comment` node only (not `analyze`); if the comment
output still contains "RETRY" after 3 attempts, the router takes the `ok`
path anyway so we never loop forever.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph

from chains import analyze_chain, comment_chain
from tools import lint_diff, run_tests


class ReviewState(TypedDict, total=False):
    diff: str
    parsed: str
    analysis: str
    comments: str
    lint_status: str
    test_status: str
    attempts: int
    review: str


_MAX_ATTEMPTS = 3


async def parse_diff_node(state: ReviewState) -> dict:
    """Trim and normalize the diff for downstream nodes."""
    parsed = (state.get("diff") or "")[:500]
    return {"parsed": parsed, "attempts": 0}


async def analyze_node(state: ReviewState) -> dict:
    """Run the analyze LCEL chain."""
    analysis = await analyze_chain.ainvoke({"diff": state.get("parsed", "")})
    return {"analysis": analysis}


async def comment_node(state: ReviewState) -> dict:
    """Run the comment LCEL chain. Increments attempts."""
    comments = await comment_chain.ainvoke({"analysis": state.get("analysis", "")})
    return {"comments": comments, "attempts": state.get("attempts", 0) + 1}


async def quality_check_node(state: ReviewState) -> dict:
    """Run lint + tests as ground-truth checks."""
    lint = await lint_diff.ainvoke({"diff": state.get("parsed", "")})
    tests = await run_tests.ainvoke({"diff": state.get("parsed", "")})
    return {"lint_status": lint, "test_status": tests}


async def format_node(state: ReviewState) -> dict:
    """Produce the final markdown review."""
    review = (
        f"## Code Review (attempts={state.get('attempts', 0)})\n\n"
        f"### Analysis\n{state.get('analysis', '')}\n\n"
        f"### Comments\n{state.get('comments', '')}\n\n"
        f"### Lint\n{state.get('lint_status', '')}\n\n"
        f"### Tests\n{state.get('test_status', '')}"
    )
    return {"review": review}


def route_after_quality(state: ReviewState) -> Literal["retry", "ok"]:
    """Retry comment generation if RETRY is in the output AND we have attempts left."""
    needs_retry = "RETRY" in (state.get("comments") or "")
    if needs_retry and state.get("attempts", 0) < _MAX_ATTEMPTS:
        return "retry"
    return "ok"


def build_graph() -> Any:
    sg: StateGraph = StateGraph(ReviewState)
    sg.add_node("parse", parse_diff_node)
    sg.add_node("analyze", analyze_node)
    sg.add_node("comment", comment_node)
    sg.add_node("quality_check", quality_check_node)
    sg.add_node("format", format_node)

    sg.set_entry_point("parse")
    sg.add_edge("parse", "analyze")
    sg.add_edge("analyze", "comment")
    sg.add_edge("comment", "quality_check")
    sg.add_conditional_edges(
        "quality_check",
        route_after_quality,
        {"retry": "comment", "ok": "format"},
    )
    sg.add_edge("format", END)
    return sg.compile()

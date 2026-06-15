"""Self-test for the bundled pytest plugin (the `tracelens_capture` fixture).

The plugin is registered via the pytest11 entry point, so the fixture is available
without any conftest wiring.
"""
from __future__ import annotations

import pytest
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.tools import tool


@tool
def search(q: str) -> str:
    """A search tool used to exercise tool capture."""
    return f"results for {q}"


def test_sync_capture_records_run(tracelens_capture) -> None:
    FakeListLLM(responses=["hello"]).invoke("hi there")
    assert tracelens_capture.runs(), "should capture at least one run"
    tracelens_capture.assert_no_errors()


def test_tool_called_assertions(tracelens_capture) -> None:
    search.invoke("hotels in paris")
    tracelens_capture.assert_tool_called("search")
    assert tracelens_capture.called_tool("search")
    assert not tracelens_capture.called_tool("missing")
    assert "search" in tracelens_capture.tool_calls()


def test_assert_tool_called_raises_when_absent(tracelens_capture) -> None:
    search.invoke("x")
    with pytest.raises(AssertionError, match="expected tool"):
        tracelens_capture.assert_tool_called("not_called")


async def test_async_capture(tracelens_capture) -> None:
    await FakeListLLM(responses=["async hi"]).ainvoke("hello async")
    assert tracelens_capture.runs(), "async invoke should be captured"
    tracelens_capture.assert_no_errors()


def test_total_tokens_returns_pair(tracelens_capture) -> None:
    FakeListLLM(responses=["hi"]).invoke("hello")
    tin, tout = tracelens_capture.total_tokens()
    assert isinstance(tin, int)
    assert isinstance(tout, int)

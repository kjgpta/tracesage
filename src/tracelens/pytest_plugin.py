"""pytest plugin: capture tracelens traces in tests and assert on agent behavior.

Enabled automatically once tracelens is installed (registered as a pytest11 entry
point). Provides the ``tracelens_capture`` fixture::

    def test_agent_uses_search(tracelens_capture):
        agent.invoke("find me a hotel")            # captured globally, no callbacks=
        tracelens_capture.assert_tool_called("search")
        tracelens_capture.assert_no_errors()
        assert tracelens_capture.total_tokens()[0] < 5000

The fixture runs a tracer on a background thread (no server), installs it as the
global LangChain handler for the duration of the test, and tears it down after.
Works for both sync and async tests.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tracelens.models import Run, StoredEvent


class TraceCapture:
    """Query + assertion helper over the events captured during a test.

    Read methods auto-flush the pipeline first, so events emitted by a just-completed
    ``invoke``/``ainvoke`` are guaranteed to be visible.
    """

    def __init__(self, bg: Any) -> None:
        self._bg = bg

    @property
    def handler(self) -> Any:
        """The callback handler, if you prefer passing ``callbacks=[...]`` explicitly."""
        return self._bg.handler

    def flush(self) -> None:
        """Block until all queued events are persisted."""
        self._bg.flush()

    def _run(self, coro: Any) -> Any:
        import asyncio

        return asyncio.run_coroutine_threadsafe(coro, self._bg._loop).result(timeout=10)

    # ----------------------------------------------------------------- queries

    def runs(self) -> list[Run]:
        """All captured runs (newest first)."""
        self.flush()
        runs, _total = self._run(self._bg.tracer.db.list_runs(limit=10_000, offset=0))
        return runs

    def events(self, run_id: str | None = None) -> list[StoredEvent]:
        """Captured events — for one run, or across all runs if `run_id` is None."""
        self.flush()
        if run_id is not None:
            return self._run(self._bg.tracer.db.get_journey(run_id))
        out: list[StoredEvent] = []
        runs, _ = self._run(self._bg.tracer.db.list_runs(limit=10_000, offset=0))
        for r in runs:
            out.extend(self._run(self._bg.tracer.db.get_journey(r.run_id)))
        return out

    def tool_calls(self) -> list[str]:
        """Names of every tool that was invoked (in event order, with repeats)."""
        return [
            e.tool_name
            for e in self.events()
            if e.event_type.value == "tool_start" and e.tool_name
        ]

    def called_tool(self, name: str) -> bool:
        return name in set(self.tool_calls())

    def errors(self) -> list[StoredEvent]:
        """All error events captured."""
        return [e for e in self.events() if e.event_type.value.endswith("_error")]

    def total_tokens(self) -> tuple[int, int]:
        """(input_tokens, output_tokens) summed across all captured events."""
        evs = self.events()
        return (
            sum(e.token_input or 0 for e in evs),
            sum(e.token_output or 0 for e in evs),
        )

    # -------------------------------------------------------------- assertions

    def assert_tool_called(self, name: str) -> None:
        if not self.called_tool(name):
            got = sorted(set(self.tool_calls()))
            raise AssertionError(f"expected tool {name!r} to be called; tools seen: {got}")

    def assert_no_errors(self) -> None:
        errs = self.errors()
        if errs:
            msgs = [e.error_message or e.summary for e in errs]
            raise AssertionError(f"expected no errors, got {len(errs)}: {msgs}")

    def assert_run_count(self, n: int) -> None:
        runs = self.runs()
        if len(runs) != n:
            raise AssertionError(f"expected {n} run(s), got {len(runs)}")


@pytest.fixture
def tracelens_capture(tmp_path: Any) -> Iterator[TraceCapture]:
    """Capture all LangChain activity during the test into an isolated tracer."""
    import tracelens
    from tracelens.config import TraceLensConfig

    cfg = TraceLensConfig(data_dir=tmp_path, print_run_url=False)
    bg = tracelens.start(cfg, start_server=False, install=True)
    try:
        yield TraceCapture(bg)
    finally:
        bg.stop()

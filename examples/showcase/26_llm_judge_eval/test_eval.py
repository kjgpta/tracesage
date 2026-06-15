"""26 — Eval regression test using the `tracelens_capture` pytest fixture.

The fixture (registered automatically by the tracelens pytest11 entry point) installs
a global LangChain handler for the test, so the graph below is captured with NO
`callbacks=` wiring — same zero-touch story as `after.py`. We then assert on the
captured trace: no errors fired, and the batch stayed under a token budget.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # real LLM calls; test skips if unset
    pytest test_eval.py
"""
from __future__ import annotations

import os

import pytest
from before import DATASET, build_graph

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
    reason="needs a real LLM provider key (OPENAI_API_KEY / ANTHROPIC_API_KEY)",
)

TOKEN_BUDGET = 5000


async def test_eval_batch_is_clean_and_cheap(tracelens_capture) -> None:
    graph = build_graph()
    for item in DATASET:
        await graph.ainvoke(
            {"question": item["question"], "expected": item["expected"],
             "answer": "", "score": 0.0, "rationale": ""}
        )

    # The fixture captured every task + judge call globally, no callbacks= needed.
    tracelens_capture.assert_no_errors()
    tracelens_capture.assert_run_count(len(DATASET))

    tok_in, tok_out = tracelens_capture.total_tokens()
    assert tok_in + tok_out < TOKEN_BUDGET, f"eval batch too expensive: {tok_in + tok_out} tokens"

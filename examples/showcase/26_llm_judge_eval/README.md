# 26 — LLM-as-Judge Eval Harness

**Domain:** ML ops · **Base:** LangGraph · **Pattern:** eval

Runs a tiny eval dataset (3 question/expected pairs) through a two-node LangGraph: a
*task* node answers each question, then a *judge* LLM scores correctness `0.0-1.0` with a
one-line rationale (Pydantic structured output). The batch loops over the dataset and
prints a score table with the average — the classic offline LLM-as-judge eval loop.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
pytest test_eval.py                  # eval regression test via the tracesage_capture fixture
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracesage import TraceSage` and wrapping the batch loop in
`async with TraceSage.session(install=True)` (plus `await tl.flush()` and a keep-the-UI-up
prompt). No `callbacks=` wiring — the session installs a global LangChain handler, so every
`graph.ainvoke` in the loop is captured automatically.

`test_eval.py` shows the same zero-touch capture in **CI**: the bundled `tracesage_capture`
pytest fixture installs the global handler for the test, then `assert_no_errors()` and
`total_tokens()` turn the trace into regression assertions (the batch must be error-free and
stay under a token budget).

## What the trace shows

- **Many runs you can compare:** each dataset item is its own root run (`task → judge`), so
  the run list holds one row per question — feed two into `tracesage diff` to compare a
  passing answer against a failing one side by side.
- The **judge's structured verdict** (`score` + `rationale`) captured as the LLM output, next
  to the task node's raw answer, so you can see exactly why a score was assigned.
- **Per-run and aggregate token usage**, making cost-per-eval and total batch spend visible.
- The same trace, asserted in **pytest** via `tracesage_capture` (`assert_no_errors`,
  `assert_run_count`, `total_tokens`) — observability that doubles as an eval test.

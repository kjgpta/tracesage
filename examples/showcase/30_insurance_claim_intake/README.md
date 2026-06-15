# 30 — Insurance Claim Intake & Routing

**Domain:** insurance · **Base:** LangGraph · **Pattern:** extract-validate-route

Takes a free-text claim description and runs it through a deterministic-rails-around-an-LLM
pipeline: an `extract` node pulls structured fields (`with_structured_output`), a `validate`
node runs a completeness check plus a simple fraud-signal heuristic, and a conditional edge
routes the claim to one of three terminal nodes — `auto_approve`, `manual_review`, or
`fraud_review`.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracelens import TraceLens` and wrapping the run in
`async with TraceLens.session(install=True)` plus an `await tl.flush()`. The
`install=True` registers a global LangChain handler, so there is no `callbacks=` wiring on
the graph itself — the graph construction is byte-identical between the two files.

## What the trace shows

- The **structured-extraction LLM call** in the `extract` node, with the prompt and the
  parsed `Claim` object (claimant, type, amount, date) in the drawer.
- The **`validate` node** and the exact `issues` list it produced — the completeness checks
  and the fraud-signal heuristic that drive the decision.
- The **3-way routing branch**: which conditional edge fired and why the claim landed in
  `auto_approve` vs `manual_review` vs `fraud_review`.
- A complete, replayable **audit trail for a regulated domain** — every input, model call,
  and routing decision captured end-to-end in one trace.

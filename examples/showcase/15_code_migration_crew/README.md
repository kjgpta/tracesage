# 15 — Code-Migration Crew

**Domain:** software eng · **Base:** LangGraph · **Pattern:** dynamic fan-out

A planner node lists the files in a tiny hardcoded repo, a transform node loops over each
file and asks the LLM to add Python type hints (one similar call per work item), and a
reviewer node summarizes the migration. A self-contained stand-in for a real codemod crew
— no repo to clone, no API beyond your LLM provider.

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

The only difference is `from tracelens import TraceLens`, wrapping the run in
`async with TraceLens.session(install=True)`, and `await tl.flush()` to drain events
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global
install captures every LangGraph node and LLM call automatically.

## What the trace shows

- **Per-work-item fan-out:** inside the `migrate` node, one transform LLM call per file —
  see each as its own span so you can spot a slow or failing item among the batch.
- **Many similar LLM calls:** the repeated type-hint prompts line up side by side, making
  latency and token usage directly comparable across files.
- **Planner → transform → review shape:** the converging LangGraph topology (`plan` →
  `migrate` fan-out → `summarize`) is visible end to end in the topology view.

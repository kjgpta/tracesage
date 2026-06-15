# 17 — Debate-to-Decision

**Domain:** decision support · **Base:** LangGraph · **Pattern:** multi-persona loop

Two persona nodes — an **optimist** and a **skeptic** — alternate across 2 rounds, each
appending one line to a shared transcript. A round counter in state drives a conditional
edge that loops the debate back to the optimist until `rounds >= 2`, then hands off to a
**judge** node that reads the full transcript and renders an ADOPT / REJECT verdict.

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
`async with TraceLens.session(install=True)`, and one `await tl.flush()` (plus a one-line
keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global handler captures
every node and LLM call automatically.

## What the trace shows

- **Looped multi-agent rounds** — the optimist→skeptic pair appears twice, so replay shows
  the back-and-forth as repeated node executions rather than one flat pass.
- The **round counter and conditional edge**: each skeptic turn increments `rounds`, and you
  can see the routing decision that re-enters the loop vs. exits to the judge.
- **Convergence** — the growing `transcript` state across rounds, then the judge LLM call
  reading the whole debate to produce a single verdict at the leaf of the graph.
- Per-node **latency and token usage**, with the full persona prompts and responses in the
  drawer — handy for spotting which round actually moved the argument.

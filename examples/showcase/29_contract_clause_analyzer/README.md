# 29 — Contract Clause Risk Analyzer

**Domain:** legal · **Base:** LangGraph · **Pattern:** parallel classify

Splits a short contract into ~4 clauses, then a `fan_out` node fires one classifier LLM
call per clause *concurrently* (`asyncio.gather`) — each returns a structured risk
`level` + `reason`. A `summarize` node folds the findings into a 3-sentence risk memo
that flags the HIGH-risk clauses first. This is the classic map/reduce shape over
independent LLM calls.

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
`async with TraceLens.session(install=True) as tl:` plus `await tl.flush()` (and a
one-line keep-the-UI-up prompt for the demo). `install=True` registers a global
LangChain handler, so there is no `callbacks=` wiring anywhere in the graph.

## What the trace shows

- **Parallel clause classification** — the `fan_out` node spawns one structured-output
  LLM call per clause, all in flight at once, so you can see them overlap on the timeline
  instead of running serially.
- The **risk flags** each call produced (low / medium / high + reason), captured as the
  structured `ClauseRisk` output of every classifier call.
- The **fan-out → summarize** topology: a node that emits N concurrent children, then a
  single summarize node that reduces them into one memo — the map then reduce boundary is
  visible as the graph narrows back to one path.

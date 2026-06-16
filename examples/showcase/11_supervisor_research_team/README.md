# 11 — Supervisor Research Team

**Domain:** research · **Base:** LangGraph · **Pattern:** supervisor

A `supervisor` node routes between three worker nodes — `researcher`, `writer`, and
`fact_checker` — via conditional edges, looping `supervisor → worker → supervisor` until
it routes to `done`. Routing is a structured-output decision (`with_structured_output`),
and the researcher answers from the model directly (no external tools), so the whole team
is self-contained.

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

The only difference is `from tracesage import TraceSage`, wrapping the run in
`async with TraceSage.session(install=True)`, and an `await tl.flush()` (plus a one-line
keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global handler captures
every node and LLM call automatically.

## What the trace shows

- The headline **multi-agent topology**: the `supervisor → researcher / writer /
  fact_checker` fan-out edges and the worker → supervisor return edges, rendered as the
  graph view.
- The supervisor's **routing decisions** — each structured-output LLM call and the `next`
  value it chose, so you can see *who acted next and why* on every loop.
- The **supervisor loop** unrolled across iterations, with the bounded `steps` cap so you
  can confirm the team terminates instead of cycling forever.
- Per-node **latency and token usage**, plus each worker's full prompt/response payload in
  the drawer (research notes → draft → fact-check verdict).

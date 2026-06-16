# 23 — Reflexion Writer

**Domain:** content · **Base:** LangGraph · **Pattern:** writer-critic loop

A writer node drafts a short paragraph; a critic node scores it 1-10 and returns one line
of feedback. A conditional edge loops back to the writer — carrying that feedback into the
next draft — until the score reaches 8 or 3 iterations elapse. A bounded reflection loop:
the classic "self-improve until good enough" shape.

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

The only difference is `from tracesage import TraceSage` and wrapping the run in
`async with TraceSage.session(install=True)` (plus `await tl.flush()` and a one-line
keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the graph code is byte-identical.

## What the trace shows

- The **bounded reflection loop** as repeated `write → critic` node visits, with the
  conditional edge looping back to `write` until the score clears 8 or the iteration cap hits.
- **Token growth per iteration**: each revision feeds the prior critic feedback into the
  writer prompt, so you can watch prompt size and token usage climb across passes.
- A full **replay of the draft → critique → revise cycle**: open each iteration to read the
  draft, the parsed `SCORE`/`FEEDBACK`, and how that score routed the next hop — making the
  loop's stopping decision visible end to end.

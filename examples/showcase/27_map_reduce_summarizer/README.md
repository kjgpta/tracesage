# 27 — Map-Reduce Long-Doc Summarizer

**Domain:** doc processing · **Base:** LangChain · **Pattern:** map-reduce

Splits a long document into ~5 chunks, then **maps** a one-sentence summarize chain over
every chunk in parallel via `chain.batch(...)`, and finally **reduces** the chunk summaries
into one tight paragraph. This is the canonical shape for summarizing text that does not fit
in a single context window.

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

The only difference is `import tracesage` and wrapping the run in `with tracesage.trace():`
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring.

## What the trace shows

- The **map fan-out**: one `batch` call expands into parallel chunk-summary LLM calls,
  shown side by side as sibling branches in the topology view.
- The single **reduce LLM call** that folds the chunk summaries into the final paragraph,
  sitting downstream of the whole map stage.
- **Token accounting across many calls** — per-chunk prompt/completion tokens plus the
  reduce step, so you can see the total cost of a map-reduce pass at a glance.
- Per-call **latency**, letting you spot the slowest chunk in the parallel fan-out.

# 07 — Map-Reduce Summarizer (with tracelens)

Identical workflow to `../../before/07_map_reduce/`, plus tracelens.

## What changes from `before/`

- **`tracelens_setup.py`** (new) — tracer init helper.
- **`main.py`** — three lines added.

`llm.py`, `tools.py`, `nodes.py`, `graph.py` are byte-identical.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

## What to look for in tracelens

- **Dynamic fan-out edge counts** — `chain:LangGraph -> agent:summarize_chunk`
  has `count = total_chunks_across_all_docs`. For the demo (3+2+2 chunks),
  that's 7. The same edge in system 02 (fixed fan-out) shows fixed counts.
- **`agent:summarize_chunk` invocation_count** — equals the total number of
  chunks across all 3 demo documents. Compare with `agent:reduce` (3 — one
  per document). The ratio summarizer/reduce > 1 is the visual signature
  of map-reduce.
- **Per-run timeline** — Doc 1's run has 3 parallel summarize_chunk steps
  in the timeline view, Doc 2 and 3 have 2 each. The horizontal-stack
  pattern is identical to system 02 but the *count* varies per run.
- **Tools** — `tool:split_text` and `tool:join_summaries` each accumulate 3
  invocations (one per doc).

## What this system spotlights

- **Dynamic concurrency** — system 02 had a fixed number of parallel
  branches (3). This one has variable parallelism per run. Without tracing
  you have to read the code to know how many summarizers ran.
- **`Send`-based dispatch is observable** — the topology shows a single
  `summarize_chunk` node, but its invocation count reflects the dynamic
  dispatch. Click the node to see all invocations across all runs.

# 09 — Error Recovery Pipeline (with tracelens)

Identical workflow to `../../before/09_error_recovery/`, plus tracelens.

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

- **`error_count` on `tool:flaky_fetch`** — the topology node for the flaky
  tool shows `error_count = 1` (call #3 raised). Other tools have
  `error_count = 0`. This is **the** place to look for systemic failures
  in production.
- **`tool_error` event in run 3's timeline** — on the failed run, you'll
  see a `tool_error` step on `flaky_fetch` with the exception text in the
  summary. Click "show full payload" for the type + repr.
- **The fallback path appears only on the failed run** — `agent:fallback_node`
  has `invocation_count = 1` (only run 3 took it). Compare: runs 1 and 2
  go directly from `fetch` to `process`.
- **Run status stays `completed`, not `failed`** — even though a tool
  errored, the graph caught it and recovered. tracelens reports the truth:
  the tool errored, the run completed. This distinction matters when
  setting up alerts.

## What this system spotlights

- **Errors are first-class events** — every `*_error` callback is captured
  as a distinct event type with full exception detail. They're queryable
  via `/api/runs?status=failed` and visible per-node in the topology.
- **Recovery paths are observable** — without tracing, "did the fallback
  fire?" requires log archaeology. With tracelens, the topology tells you
  immediately.

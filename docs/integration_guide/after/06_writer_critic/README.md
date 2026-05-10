# 06 — Writer-Critic Loop (with tracelens)

Identical workflow to `../../before/06_writer_critic/`, plus tracelens.

## What changes from `before/`

- **`tracelens_setup.py`** (new) — tracer init helper.
- **`main.py`** — three lines added.

`llm.py`, `tools.py`, `agents.py`, `graph.py` are byte-identical.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

## What to look for in tracelens

- **Cyclic edge counts** — `agent:writer` and `agent:critic` connect through
  `chain:LangGraph`. The writer's `invocation_count` is 4 across the demo
  (1 + 2 + 1 attempts); the critic matches.
- **Topic 2's run timeline** — open the run for "observability for AI apps"
  and you'll see two writer steps, two critic steps, and a finalize step.
  Compare to topics 1 and 3 (one of each).
- **Critic's tools** — `tool:word_count` and `tool:readability_check` show
  invocation counts equal to the critic's invocation count (4 each), since
  the critic always runs both tools.
- **`route_after_critic` chain** — the routing function appears as
  `chain:route_after_critic` because LangGraph wraps named edge functions.

## What this system spotlights

- **Cyclic graphs are visible** — many self-correcting agent systems share
  this writer-critic shape. Without traces, you can't see how many revision
  cycles a given input triggered. tracelens makes this immediate.
- **Quality gates as tools** — `word_count` / `readability_check` are
  rendered as their own topology nodes, distinct from LLM calls. This is
  the right cue when ground-truth checks (your "critic's eyes") are tools
  rather than another LLM call.

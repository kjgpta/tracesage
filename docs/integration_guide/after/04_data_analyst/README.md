# 04 — Data Analyst Multi-Agent (with tracelens)

Identical workflow to `../../before/04_data_analyst/`, plus tracelens.

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

- **Supervisor at the center** — `agent:supervisor` connects bidirectionally
  to the three workers. Edge counts on the worker → supervisor edges grow
  per worker invocation.
- **Per-question worker composition** — Q1's run shows only `sql_agent` in
  the timeline; Q2 shows `sql_agent + chart_agent`; Q3 shows all three.
- **Supervisor invocation count** — equals (workers + 1) per question. With
  the 3 demo questions (1 + 1 = 2, 2 + 1 = 3, 3 + 1 = 4), supervisor's
  `invocation_count` lands at 9.
- **Tools per worker** — each worker connects to its own toolbox. The
  topology makes the agent-tool affinity obvious without reading the code.

## What this system spotlights

- **The supervisor pattern made legible** — supervisor systems are notoriously
  hard to debug from logs. tracelens turns the bidirectional flow into a
  visible loop in the graph.
- **Conditional routing without retries** — unlike system 03's retry edge,
  here the loop is shaped by the supervisor's per-step decision, not error
  recovery. The topology distinguishes the two kinds of cycles by which
  edges are traversed in which order per run.

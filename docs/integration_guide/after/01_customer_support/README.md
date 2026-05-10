# 01 — Customer Support Triage (with tracelens)

Identical workflow to `../../before/01_customer_support/`, plus tracelens.

## What changes from `before/`

Two files are different:

1. **`tracelens_setup.py`** (new) — tracer init helper + default tags.
2. **`main.py`** — three lines added:
   ```python
   from tracelens_setup import DEFAULT_TAGS, init_tracer

   tracer = await init_tracer()
   # ...
   config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS}
   ```

`llm.py`, `tools.py`, `agents.py`, `graph.py` are byte-identical to `before/`.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

Then open `http://localhost:7842/ui` and explore the runs. The script keeps the
server up after the queries finish so you can browse — Ctrl+C to stop.

Real LLM:

```bash
LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

## What to look for in tracelens

- **Topology graph**: the three specialist agents
  (`agent:billing_agent`, `agent:tech_agent`, `agent:escalation_agent`) each
  connect to a different toolbox. The `chain:LangGraph` orchestrator sits at
  the top. The conditional `triage → router` branch is visible — different
  runs light up different specialists.
- **Run list**: 4 runs, all tagged `customer-support`, all `completed`.
- **Step detail**: click any LLM step's "show full payload" to see the prompt
  and response (gzipped on disk, lazy-loaded).
- **Filter**: type `customer-support` in the run-list search to isolate this
  system's runs from others sharing `~/.tracelens`.

## What this system spotlights

- **Mixed-framework topology** — LangGraph is the orchestrator, but every
  internal step (LLM, tool) is a LangChain primitive. tracelens renders both
  cleanly in one graph.
- **Conditional routing visible** — the same triage node sends different
  queries to different specialists. The unused branches per run are visually
  faded in the graph.

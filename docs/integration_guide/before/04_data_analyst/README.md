# 04 — Data Analyst Multi-Agent (before tracelens)

A LangGraph **supervisor pattern**: one orchestrator routes work to specialized
worker agents and aggregates their outputs.

## Architecture

```
supervisor → router → { sql_agent | chart_agent | narrative_agent | finalize }
                                         ↓ (each worker returns to supervisor)
                                     supervisor (loop until done)
```

- **supervisor** picks the next worker (or `done`) based on the question
- **sql_agent** uses `fetch_schema` + `run_sql`
- **chart_agent** uses `plot_chart`
- **narrative_agent** uses `write_summary`
- **finalize** assembles whatever workers contributed into a final answer

The 3 demo questions exercise different worker compositions:
- Q1 ("user signups") → sql only
- Q2 ("revenue trend") → sql + chart
- Q3 ("quarterly review") → sql + chart + narrative

## Run

```bash
pip install langchain-core langgraph
python main.py
```

Real LLM:

```bash
LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

## Files

- `llm.py` — provider switch
- `tools.py` — `fetch_schema`, `run_sql`, `plot_chart`, `write_summary`
- `agents.py` — supervisor + 3 worker agents + finalize
- `graph.py` — supervisor loop wiring
- `main.py` — entry point with 3 questions

# 09 — Error Recovery Pipeline (before tracelens)

A LangGraph pipeline where the primary fetch tool fails on a deterministic
schedule. The graph routes through a fallback when an error occurs, then
continues processing normally.

## Architecture

```
fetch → router (success | error)
          ├ success → process → summarize → END
          └ error   → fallback → process → summarize → END
```

- **fetch** calls `flaky_fetch` (raises every 3rd call, deterministic)
- **router** branches on `state["error"]`
- **fallback** calls the reliable `fallback_fetch`
- **process** calls `process_data`
- **summarize** runs an LLM to summarize the run

The 3 demo URLs exercise both paths: calls 1 and 2 succeed; call 3 raises
and triggers the fallback.

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
- `tools.py` — `flaky_fetch` (raises), `fallback_fetch`, `process_data`
- `nodes.py` — `fetch_node`, `fallback_node`, `process_node`, `summarize_node`
- `graph.py` — LangGraph wiring with error-conditional edge
- `main.py` — entry point with 3 URLs (one of which triggers the fallback)

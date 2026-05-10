# 06 — Writer-Critic Loop (before tracelens)

A self-correcting two-agent loop. The writer drafts; the critic scores. On
`REVISE`, the loop returns to the writer with the critic's feedback.

## Architecture

```
writer → critic → router
                    ├ PASS  → finalize → END
                    └ REVISE (attempts < 3) → writer (loop)
```

- **writer** generates a draft (or revises one given critic feedback)
- **critic** emits a verdict (`PASS` / `REVISE: ...`) plus runs ground-truth
  tools (`word_count`, `readability_check`)
- **finalize** wraps the accepted draft with metadata

The 3 demo topics exercise different paths:
- Topic 1 passes immediately (1 attempt)
- Topic 2 requires one revision (2 attempts)
- Topic 3 passes immediately (1 attempt)

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
- `tools.py` — `word_count`, `readability_check`
- `agents.py` — writer, critic, finalize
- `graph.py` — cyclic LangGraph wiring
- `main.py` — entry point with 3 topics

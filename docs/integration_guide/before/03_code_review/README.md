# 03 — Code Review Assistant (before tracelens)

A code review pipeline built from two **LangChain LCEL chains** wrapped in a
**LangGraph state machine** with a retry edge.

## Architecture

```
parse → analyze (LCEL) → comment (LCEL) → quality_check → router
                                                            ├─ retry → comment
                                                            └─ ok    → format → END
```

- **parse** trims and normalizes the diff
- **analyze** is an LCEL chain: `prompt | LLM | parser`
- **comment** is another LCEL chain
- **quality_check** runs `lint_diff` + `run_tests` as tools
- **router** decides whether to retry comment generation (max 3 attempts)
- **format** assembles the final markdown review

The 3 sample diffs are crafted to exercise different paths:
- Diff 1 passes immediately
- Diff 2 triggers one retry (the fake LLM emits `RETRY` on attempt 1, then a
  clean comment on attempt 2)
- Diff 3 passes immediately

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
- `tools.py` — `lint_diff`, `run_tests`
- `chains.py` — two LCEL `prompt | llm | parser` pipelines
- `graph.py` — LangGraph wiring with the retry edge
- `main.py` — entry point with 3 sample diffs

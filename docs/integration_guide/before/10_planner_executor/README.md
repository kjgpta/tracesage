# 10 — Planner-Executor (before tracelens)

A planner-executor loop. The planner emits a list of steps once; the
executor runs each step iteratively until the plan is empty.

## Architecture

```
planner → executor → router
                       ├ done → END
                       └ next → executor (loop)
```

- **planner** runs ONCE per task and emits a comma-separated step list:
  `search,read,notes,synthesize`
- **executor** runs ONCE per step. Each step pops the head of the plan and
  dispatches to a matching tool (`search`, `read_doc`, `take_notes`,
  `synthesize`).
- **router** loops back to `executor` until the plan is empty.

The 3 demo tasks have plan lengths 4, 3, 2 — so the executor runs 9 times
total (4 + 3 + 2).

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
- `tools.py` — `search`, `read_doc`, `take_notes`, `synthesize`
- `agents.py` — `planner_node`, `executor_node` + the step-tool dispatch
- `graph.py` — LangGraph wiring with the executor loop
- `main.py` — entry point with 3 tasks of differing plan lengths

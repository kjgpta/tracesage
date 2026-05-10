# 01 — Customer Support Triage (before tracelens)

A LangGraph state machine that routes customer queries to specialist agents.
Each specialist combines a tool-selection LLM, a tool invocation, and a
reply-formatting LLM.

## Architecture

```
[customer query] → triage → router → { billing | tech | escalate } → END
```

- **triage** classifies the query (billing / tech / escalate)
- **billing_agent** picks one of `lookup_account`, `issue_refund`, `check_balance`
- **tech_agent** picks one of `run_diagnostic`, `restart_service`, `check_logs`
- **escalation_agent** hands off to a human, no tools

## Run

```bash
pip install langchain-core langgraph
python main.py
```

Switch to a real LLM:

```bash
LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

## Files

- `llm.py` — provider switch (fake / openai / anthropic)
- `tools.py` — 6 LangChain tools (3 billing, 3 tech)
- `agents.py` — specialist agent nodes + the shared `_specialist` pattern
- `graph.py` — LangGraph state machine wiring
- `main.py` — entry point with 4 sample queries

## What's missing

You have no visibility into:

- Which tool each specialist picked, and why
- How long each LLM call took
- The full prompts and responses (only the final summary is printed)
- Whether the routing matched what you expected for each query
- Per-step error rates and retry behavior

That's what `../../after/01_customer_support/` adds with three lines of code.

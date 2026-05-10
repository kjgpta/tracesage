# 08 — Streaming Token Agent (before tracelens)

A LangGraph pipeline that streams tokens from an LCEL chain and post-processes
the result with two tools.

## Architecture

```
streamed_answer (LCEL chain.astream) → followup (shorten + add_disclaimer) → END
```

- **streamed_answer** runs `streaming_chain.astream(...)` and accumulates
  chunks. With `FakeListChatModel`, you get one chunk; with a real
  provider that supports streaming, you get many.
- **followup** trims the streamed text and appends a disclaimer (two tool
  calls in sequence).

## Run

```bash
pip install langchain-core langgraph
python main.py
```

For the streaming spotlight, use a real model with `streaming=True` (the
chain code enables it automatically when `LLM_PROVIDER` is `openai` or
`anthropic`):

```bash
LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

## Files

- `llm.py` — provider switch
- `tools.py` — `shorten`, `add_disclaimer`
- `chains.py` — LCEL streaming chain (`prompt | llm | parser`) with
  streaming enabled per provider
- `graph.py` — two-node LangGraph
- `main.py` — entry point with 3 questions

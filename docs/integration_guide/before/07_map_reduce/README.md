# 07 — Map-Reduce Summarizer (before tracelens)

A LangGraph pipeline that splits a long document into chunks, summarizes each
chunk in parallel via dynamic fan-out, then reduces.

## Architecture

```
split → fan-out via Send → N parallel summarize_chunk → reduce → END
```

- **split** uses `split_text` to chop the document into ~200-char chunks
- **fan-out** — `add_conditional_edges` returns a list of `Send("summarize_chunk", {chunk})`,
  one per chunk; LangGraph dispatches them concurrently
- **summarize_chunk** runs the summary LLM on one chunk; writes to a
  `Annotated[list, operator.add]` state field so all results accumulate
- **reduce** joins summaries via `join_summaries` and runs a final LLM pass

The number of parallel branches is **dynamic** — set by chunk count, not
hardcoded. Different documents produce different fan-out widths.

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
- `tools.py` — `split_text`, `join_summaries`
- `nodes.py` — `split_node`, `summarize_chunk`, `reduce_node`
- `graph.py` — LangGraph wiring with `Send`-based dynamic fan-out
- `main.py` — entry point with 3 documents of different lengths

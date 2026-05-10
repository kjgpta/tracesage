# 02 — Document Research Pipeline (before tracelens)

A LangGraph pipeline with **parallel fan-out** for document analysis.

## Architecture

```
ingest → retrieve → ┬→ fact_extractor ─┐
                    ├→ sentiment       ├→ synthesize → END
                    └→ entities        ┘
```

- **ingest** plans the search and fetches a doc (uses `web_search` +
  `fetch_document` tools)
- **retrieve** runs a LangChain `BaseRetriever` against a fixed corpus
- **fact_extractor**, **sentiment**, **entities** run **in parallel**
- **synthesize** merges results and adds citations via `cite_sources`

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
- `tools.py` — 3 LangChain tools (web_search, fetch_document, cite_sources)
- `nodes.py` — 6 graph nodes including the parallel branches + retriever
- `graph.py` — LangGraph wiring with the fan-out / fan-in shape
- `main.py` — entry point with 3 sample topics

## What's missing

You can't see:

- That the three analyzers actually ran in parallel (the script's print order
  is just the final state — not the execution timeline)
- How long each analyzer took
- Which retrieved documents drove which analyzer's output
- Whether the synthesize step received all three branch outputs as expected

That's what `../../after/02_research_pipeline/` adds.

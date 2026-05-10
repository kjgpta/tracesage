# 05 — RAG with Reranker (before tracelens)

A two-stage retrieval pipeline: fast retriever returns many candidates, an
LLM reranker scores them, the top 3 feed the answer chain.

## Architecture

```
retrieve (BaseRetriever) → rerank (LCEL) → answer (LCEL + cite_sources tool) → END
       │                       │                  │
       │                       │                  └ prompt | LLM | parser
       │                       └ prompt | LLM | parser
       └ returns 8 candidates from a fixed corpus
```

- **retrieve** — `FastFakeRetriever` (a `BaseRetriever` subclass) returns 8
  candidates; in production this is FAISS / Chroma / Elastic / etc.
- **rerank** — LCEL chain that scores candidates and returns indices for the top 3
- **answer** — LCEL chain that generates a grounded answer, then adds
  citations via the `cite_sources` tool

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
- `tools.py` — `cite_sources`
- `retrievers.py` — `FastFakeRetriever` + the corpus
- `chains.py` — `rerank_chain`, `answer_chain` (both LCEL)
- `graph.py` — three-node LangGraph
- `main.py` — entry point with 3 sample questions

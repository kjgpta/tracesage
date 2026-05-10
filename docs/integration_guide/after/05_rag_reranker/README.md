# 05 — RAG with Reranker (with tracelens)

Identical workflow to `../../before/05_rag_reranker/`, plus tracelens.

## What changes from `before/`

- **`tracelens_setup.py`** (new) — tracer init helper.
- **`main.py`** — three lines added.

`llm.py`, `tools.py`, `retrievers.py`, `chains.py`, `graph.py` are byte-identical.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

## What to look for in tracelens

- **Retriever events as a distinct kind** — `retriever:FastFakeRetriever`
  appears as its own topology node, not a generic `agent:` or `tool:`. Click
  any retriever step and "show full payload" reveals the candidate list with
  scores from the `metadata` dict.
- **Two LCEL chains** — both `rerank_chain` and `answer_chain` decompose into
  `chain:RunnableSequence` → `chain:ChatPromptTemplate` + `llm:...` +
  `chain:StrOutputParser`. The topology shows you exactly which chain owns
  which pipe.
- **Multi-stage retrieval visible** — the topology shape `retrieve →
  rerank → answer` tells you at a glance that this is a two-stage pipeline.
  Compare with system 02 (single-stage retrieval) to see the difference.

## What this system spotlights

- **Retriever as a first-class topology kind** — once you have multiple
  retrieval stages (vector + reranker, hybrid + filtering, etc.), being
  able to see them as distinct nodes is the difference between debugging
  and guessing.
- **Production RAG pipelines are inherently multi-stage** — reranking is one
  of the most common upgrades to a basic RAG, and tracelens visualizes the
  upgrade clearly.

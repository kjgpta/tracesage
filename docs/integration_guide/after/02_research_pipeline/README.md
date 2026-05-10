# 02 — Document Research Pipeline (with tracelens)

Identical workflow to `../../before/02_research_pipeline/`, plus tracelens.

## What changes from `before/`

Two files are different:

1. **`tracelens_setup.py`** (new) — tracer init helper + default tags.
2. **`main.py`** — three lines added (import, `init_tracer()`, `config=`).

`llm.py`, `tools.py`, `nodes.py`, `graph.py` are byte-identical to `before/`.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

Real LLM:

```bash
LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

## What to look for in tracelens

- **Topology graph**: the fan-out shape is the most distinctive feature.
  Three parallel edges leave `agent:retrieve` heading into `agent:fact_extractor`,
  `agent:sentiment`, and `agent:entities`. Three converging edges flow into
  `agent:synthesize`. This shape only emerges from real parallel execution —
  if any of the branches were serialized, the graph would look like a chain.
- **Timeline view**: the three parallel nodes' LLM calls overlap in time.
  Their bars in the timeline view will visibly stack horizontally rather than
  stretching end-to-end.
- **Retriever node**: `retriever:_FixedCorpusRetriever` is its own kind of
  topology node, separate from `agent:` and `llm:`. It accumulates one
  invocation per topic — three after running the demo's 3 topics.
- **Tools as their own column**: `tool:web_search`, `tool:fetch_document`,
  `tool:cite_sources` each appear as `tool:` nodes. The `cite_sources` tool
  is invoked from `synthesize`, while the others are invoked from `ingest`.

## What this system spotlights

- **Concurrent execution visualized** — parallel branches in LangGraph are
  hard to verify without tracing. The topology + timeline together prove
  whether your fan-out is actually concurrent.
- **Retriever events as first-class** — tracelens treats retriever
  start/end as a distinct event type, surfacing them as their own topology
  kind. This matters once you have multiple retrievers (FAISS, Chroma,
  reranker pipelines — see system 5) and need to compare them.

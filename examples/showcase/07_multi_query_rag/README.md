# 07 — Multi-Query RAG

**Domain:** search · **Base:** LangGraph · **Pattern:** query-expansion fan-out

A LangGraph RAG pipeline that expands one question into three search-query variants,
retrieves for each against a small local Chroma store, fuses and de-duplicates the hits,
then answers from the merged context. Query expansion lifts recall over a single
retrieval — and the fan-out/merge shape is what makes the trace interesting.

## Run

```bash
pip install -r ../requirements.txt   # needs langchain-chroma, chromadb, langchain-openai
export OPENAI_API_KEY=...             # embeddings + chat both use OpenAI by default
python before.py                      # plain app
python after.py                       # same app + live trace UI
```

The docs are hardcoded in the script, so the Chroma store is built in-memory — no data
files or external services needed. Both chat and embeddings default to OpenAI; the
`OPENAI_API_KEY` is required even if you switch `LLM_PROVIDER` for the chat model.

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracesage import TraceSage`, wrapping the run in
`async with TraceSage.session(install=True):`, and an `await tl.flush()` so events land
before the graph returns (plus a one-line keep-the-UI-up prompt for the demo). No
`callbacks=` wiring on the graph or the retriever.

## What the trace shows

- The **query-expansion LLM call** and the three variants it produced — expansion made
  visible, so you can see what was actually searched for.
- **Three parallel retriever calls** (one per variant) fanning out under the `retrieve`
  node, then the **fuse/merge step** that flattens and de-duplicates their hits.
- The fused context handed to the final **answer LLM call**, so you can trace each
  retrieved snippet through to the grounded answer.
- Per-node **latency and token usage** across the `expand → retrieve → answer` graph
  topology, with full prompt/response payloads in the drawer.

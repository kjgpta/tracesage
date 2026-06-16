# 09 — RAG + Reranker

**Domain:** search quality · **Base:** LangChain · **Pattern:** retrieve-rerank-cite

Retrieves the top-8 chunks from a small local Chroma store, then runs an LLM-as-reranker
step that scores and reorders them down to the top-3, and finally answers the question
with inline bracket citations like `[1]`. The rerank step is deliberately isolated so a
real cross-encoder (e.g. a `BAAI/bge` reranker) could swap straight in where the LLM
scorer is today — no other change to the pipeline.

## Run

```bash
pip install -r ../requirements.txt   # needs langchain-chroma + chromadb + langchain-openai
export OPENAI_API_KEY=...             # OpenAIEmbeddings needs an OpenAI key
python before.py                      # plain app
python after.py                       # same app + live trace UI
```

`OpenAIEmbeddings` requires an OpenAI key even if you point the chat model at another
provider; the Chroma store is built from a handful of hardcoded documents, so it is fully
self-contained.

## The integration

```bash
diff before.py after.py
```

The only difference is `import tracesage` and wrapping the run in `with tracesage.trace():`
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the build
and chain code are byte-identical.

## What the trace shows

- The **retriever node** pulling the top-8 candidates from Chroma, with the embedding
  query and the matched chunks visible in the drawer.
- A **distinct rerank step** — a separate LLM call that scores the 8 candidates and emits
  the top-3 indices — sitting between retrieval and answering as its own node.
- How **reranking changes which chunks ground the answer**: you can compare the retriever's
  top-8 against the 3 the reranker actually forwarded, and trace each bracket citation in
  the final answer back to a surviving chunk.
- Per-step **latency and token usage** for the retrieve / rerank / answer LLM calls.

This app makes the retrieve-vs-rerank distinction concrete: the topology view shows the
reordering as its own node, not hidden inside the retriever.

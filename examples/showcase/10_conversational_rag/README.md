# 10 — Conversational RAG (memory)

**Domain:** assistant · **Base:** LangGraph · **Pattern:** multi-turn sessions

A history-aware RAG assistant built on a LangGraph state machine with a `MemorySaver`
checkpointer. Each turn flows `rewrite → retrieve → answer`: the `rewrite` node folds the
chat history into a standalone search query (so "how much does it cost?" becomes "TraceSage
pricing"), `retrieve` pulls the top matches from a small local Chroma store, and `answer`
responds from that context. `main()` runs three turns on the same `thread_id`, so memory
carries the topic forward across turns.

## Run

```bash
pip install -r ../requirements.txt   # needs langchain-chroma, chromadb, langchain-openai
export OPENAI_API_KEY=...            # used for both chat and embeddings
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracesage import TraceSage`, wrapping the turn loop in
`async with TraceSage.session(install=True)`, and one `await tl.flush()` (plus a keep-the-UI-up
prompt for the demo). No `callbacks=[...]` threading into the graph.

## What the trace shows

- **Multi-turn sessions** — three separate `graph.ainvoke` runs that share one `thread_id`,
  so you can see the linked turns of a single conversation side by side.
- **History-aware query rewriting** — the `rewrite` node's LLM call, with the full chat
  history going in and the standalone query coming out, makes follow-ups like "and how do I
  install it?" resolvable.
- The per-node graph topology (`rewrite → retrieve → answer`) and the **Chroma retrieval**
  step, with the matched documents that grounded each answer.
- Per-step **latency and token usage**, and full prompt/response payloads in the drawer —
  handy for spotting a rewrite that dropped context between turns.

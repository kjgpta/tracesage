# 08 — Agentic RAG

**Domain:** knowledge · **Base:** LangGraph · **Pattern:** retrieval loop

A LangGraph agent that retrieves from a small local Chroma store, then **grades** the
retrieved docs for relevance. If they are not relevant, a **rewrite** node reformulates the
query and retrieves again — a conditional loop capped at 2 retries — before the answer node
responds. Topology: `retrieve → grade → (rewrite → retrieve)* → answer`.

## Run

```bash
pip install -r ../requirements.txt   # needs langchain-chroma + chromadb
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracelens import TraceLens` and wrapping the run in
`async with TraceLens.session(install=True)` (plus `await tl.flush()` and a keep-the-UI-up
prompt for the demo). No `callbacks=` wiring — the global handler captures every node.

## What the trace shows

- The **variable-depth retrieval loop**: each `retrieve` pass is a distinct span, so you
  see exactly how many turns the question took before answering.
- The **grade and rewrite decision nodes** — read the grade verdict (yes/no) that steered
  each turn and the reformulated query the rewrite node produced.
- The **conditional edges** in the topology view: the `grade → rewrite` vs `grade → answer`
  branch, plus the `rewrite → retrieve` back-edge that forms the loop.
- Per-node **latency and token usage**, and the full prompt/doc payloads in the drawer.

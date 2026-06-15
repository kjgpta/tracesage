# 14 — Competitive Intelligence Crew

**Domain:** strategy · **Base:** LangGraph · **Pattern:** parallel agents + synthesis

Two scout nodes fan out from `START` and run concurrently: `scout_a` gathers pricing signal
and `scout_b` gathers product signal, each via a free DuckDuckGo web search plus a small
scout LLM call. Both edges converge on a single `analyst` node (fan-in) that synthesizes a
short competitive brief. A textbook fan-out / fan-in graph with a synthesis barrier.

## Run

```bash
pip install -r ../requirements.txt          # needs duckduckgo-search + langchain-community
export OPENAI_API_KEY=...                    # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                             # plain app
python after.py                              # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracelens import TraceLens`, wrapping the run in
`async with TraceLens.session(install=True)`, and a single `await tl.flush()`. The
`install=True` registers a global LangChain handler, so there is no `callbacks=` wiring —
the graph construction is byte-identical between the two files.

## What the trace shows

- **Concurrent agent branches** — `scout_a` and `scout_b` running in parallel off `START`,
  their spans overlapping in time rather than running back-to-back.
- The **fan-in / synthesis barrier** — both scouts joining at the `analyst` node, which
  only fires once both branches complete.
- Each scout's **DuckDuckGo tool call** feeding its **scout LLM call**, then the analyst's
  synthesis LLM call — the full nested topology of a multi-agent crew.
- Per-node **latency and token usage**, so you can see which branch was the slow path into
  the barrier and the prompt/response payloads in the drawer.

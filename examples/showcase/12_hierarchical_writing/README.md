# 12 — Hierarchical Writing Org

**Domain:** content ops · **Base:** LangGraph · **Pattern:** nested subgraphs

A top-level "org" graph whose two team nodes are themselves *compiled* LangGraph
subgraphs: an `outline_team` (brainstorm → structure) hands off to a `draft_team`
(write → polish), and a final `edit` node titles the piece. Each team owns its own
2-node pipeline, so the whole run is a graph of graphs.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracelens import TraceLens`, wrapping the run in
`async with TraceLens.session(install=True)`, and one `await tl.flush()` (plus a
one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global
handler captures every layer of every subgraph automatically.

## What the trace shows

- **Deep nesting in the topology:** the org graph at the top, with `outline_team`
  and `draft_team` expanding into their own inner nodes (`brainstorm`/`structure`,
  `write`/`polish`) and each node's LLM call beneath that.
- **Replaying through multiple layers:** expand a team node to drill into its
  subgraph, then into the individual chain + LLM spans — state (`outline` → `draft`
  → `final`) threads through every level.
- Per-step **latency and token usage**, and the full prompt/response payloads in the
  drawer, so you can see exactly what each team contributed.

This is the gallery's clearest look at nested compiled graphs — how a graph-of-graphs
renders as a single, fully expandable trace tree.

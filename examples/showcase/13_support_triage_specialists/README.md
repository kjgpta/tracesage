# 13 — Support Triage + Specialists

**Domain:** customer support · **Base:** LangGraph · **Pattern:** specialist routing

A support desk modeled as a LangGraph. A `triage` node classifies an inbound ticket
(`billing / technical / account`) with structured output, then a conditional edge routes
it to the matching specialist node. Each specialist decides — via its own conditional
edge — whether it can resolve the ticket or must hand off to a shared `escalate` node.
The bundled ticket (a crash with a `0xC0000005` access-violation code) lands on the
**technical** specialist.

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

The only difference is `from tracelens import TraceLens` and wrapping the run in
`async with TraceLens.session(install=True)` plus a one-line `await tl.flush()`. Because
`install=True` registers a global LangChain handler, there is no `callbacks=` wiring on
the graph — the graph construction is byte-identical between the two files.

## What the trace shows

- **Routing among specialist agents** — the triage classifier call, then the single
  specialist node (billing / technical / account) the conditional edge selected.
- **The escalation branch** — each specialist's own conditional edge to either `END`
  (resolved) or the shared `escalate` node, so you can see when a human handoff fired.
- **Which path a ticket took** — the exact node sequence through the graph
  (`triage → technical → …`), with per-node latency, token usage, and the structured
  `Triage` / `Handling` payloads in the drawer.

This app is the LangGraph counterpart to app 01's LCEL router: same domain, but the
trace surfaces graph topology and conditional-edge decisions instead of an LCEL branch.

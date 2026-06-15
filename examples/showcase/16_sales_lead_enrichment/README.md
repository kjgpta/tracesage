# 16 — Sales Lead Enrichment & Outreach

**Domain:** sales/CRM · **Base:** LangGraph · **Pattern:** enrich-qualify-draft

A B2B sales pipeline as a LangGraph. The `enrich` node calls a local `fake_crm_lookup`
tool for canned firmographics; `qualify` has an LLM produce a structured fit score; and a
conditional edge routes the lead to `draft_outreach` (write a cold opener) when it's a fit,
or `disqualify` otherwise.

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
`async with TraceLens.session(install=True)` (plus `await tl.flush()` and a keep-the-UI-up
prompt). `install=True` registers a global LangChain handler — no `callbacks=` wiring.

## What the trace shows

- The **`fake_crm_lookup` tool call** in the `enrich` node, with its company input and the
  firmographics it returned.
- The **structured `qualify` LLM call** producing the `Verdict` (qualified + reason) — so you
  can see the fit score that drives routing.
- Which **conditional edge fired** (`draft_outreach` vs `disqualify`) as a distinct graph
  path through the topology view.
- The full **enrich → qualify → draft** B2B flow, node by node, with per-node latency,
  token usage, and the prompt/response payloads in the drawer.

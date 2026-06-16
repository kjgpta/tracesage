# 01 — Support FAQ Router

**Domain:** customer support · **Base:** LangChain (LCEL) · **Pattern:** classify-then-branch

Classifies an inbound support question into `billing / technical / account / other`, then
routes it through `RunnableBranch` to the matching specialist answer chain (or a human
escalation message).

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

The only difference is `import tracesage` and wrapping the run in `with tracesage.trace():`
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring.

## What the trace shows

- The **classifier LLM call** and its output category — so you can see *why* a question
  routed where it did.
- Which **branch fired** (billing/technical/account/escalation) as a distinct chain path.
- Per-step **latency and token usage**, and the full prompt/response payloads in the drawer.

This is the simplest topology in the gallery — a great first look at the run-trace and
topology views.

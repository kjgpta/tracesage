# 04 — Marketing Copy Generator

**Domain:** marketing · **Base:** LangChain (LCEL) · **Pattern:** sequential chain

A 3-stage prompt pipeline: draft → 3 headline variants → polished final with a
call-to-action. No tools, no agent — just a clean LCEL sequence.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...
python before.py
python after.py
```

## The integration

```bash
diff before.py after.py
```

## What the trace shows

- The **three chain stages laid out in order**, each its own LLM node with latency and
  token counts — so you can see which stage is slow or token-hungry.
- Full prompt + response for each stage in the drawer, perfect for iterating on a
  multi-step prompt without adding `print` statements everywhere.

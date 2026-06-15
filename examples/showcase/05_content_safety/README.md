# 05 — Content Safety Pipeline

**Domain:** trust & safety · **Base:** LangChain (LCEL) · **Pattern:** parallel fan-out

Runs three classifiers — toxicity, PII, policy — **concurrently** over a piece of content
with `RunnableParallel`, then aggregates a single ALLOW/BLOCK verdict.

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

- The three checks as **concurrent branches** in the topology (not a sequential chain),
  each LLM node with its own latency — so you can verify the fan-out really is parallel
  and find the slowest classifier on the critical path.
- The aggregation step pulling the three verdicts together.

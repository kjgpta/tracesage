# 28 — Invoice / Expense Extractor

**Domain:** finance/AP · **Base:** LangChain · **Pattern:** structured output + validation

Extracts a structured `Invoice` (vendor, date, line items, total) from messy invoice
text with `llm.with_structured_output(Invoice)`, then applies a business rule —
do the line-item amounts sum to the stated total? — and prints **PASS / FAIL**. Runs
two cases: a clean invoice that reconciles and a messy one whose total does not match
its line items.

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

The only difference is `import tracelens` and wrapping the run in `with tracelens.trace():`
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring.

## What the trace shows

- The **structured-output extraction** for each invoice: the prompt, the underlying LLM
  call, and the parsed `Invoice` object returned by `with_structured_output`.
- The **two runs side by side** — the clean invoice and the messy one — so you can compare
  what the model extracted from well-formed vs. noisy text.
- The exact **line items and total** the model pulled out, which is what the downstream
  PASS / FAIL validation reconciles — making it obvious whether a FAIL came from a bad
  extraction or a genuinely inconsistent invoice.
- Per-call **latency and token usage**, and the full prompt/response payloads in the drawer.

A focused look at the extraction → validate pattern: the trace surfaces the structured
payload that the business rule then judges.

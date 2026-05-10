# 08 — Streaming Token Agent (with tracelens)

Identical workflow to `../../before/08_streaming_agent/`, plus tracelens.

## What changes from `before/`

- **`tracelens_setup.py`** (new) — tracer init helper.
- **`main.py`** — three lines added.

`llm.py`, `tools.py`, `chains.py`, `graph.py` are byte-identical.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

For the most informative streaming view, use a real model:

```bash
LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

## What to look for in tracelens

- **Streaming telemetry on `LLM_END`** — open any LLM step's full payload
  (the `_stream` field). It contains:
  - `streamed_token_count` — total tokens received
  - `first_token_ts` — when the first token arrived
  - `stream_duration_ms` — first-token to last-token elapsed
  Compare with non-streaming systems (any of the others) where this field
  is absent.
- **Tokens-per-second visible** — the LLM step summary includes
  `streamed=<N> stream_dur=<ms> tps=<X>` once the stream finishes.
- **`token_output` populated** — even if the model doesn't emit a token-usage
  block in `llm_output`, tracelens fills `token_output` from the streamed
  chunk count. So `total_tokens_output` on the run row is non-zero even
  for fake / locally-run models.

## What this system spotlights

- **TTFT and stream length** — production streaming agents care about TTFT
  (time-to-first-token), per-token cost, and streamed length. tracelens
  records all three on the `LLM_END` event without any extra wiring.
- **Streaming integration is callback-driven** — the only requirement is
  that the model fires `on_llm_new_token`. Both real models with
  `streaming=True` and the fake model do this; tracelens captures it
  uniformly.

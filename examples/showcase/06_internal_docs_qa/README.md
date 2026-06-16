# 06 — Internal Docs Q&A

**Domain:** enterprise knowledge · **Base:** LangChain · **Pattern:** retrieve-then-answer

RAG over a tiny, self-contained product FAQ. The app builds a local Chroma store from ~5
hardcoded `Document` snippets, retrieves the top-k most relevant chunks for a question,
stuffs them into a prompt, and asks the LLM to answer **using only that context** while
**citing** the bracketed source ids it used.

## Run

```bash
pip install -r ../requirements.txt   # includes langchain-chroma + chromadb
export OPENAI_API_KEY=...            # required: OpenAIEmbeddings + answer LLM
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

> Uses `OpenAIEmbeddings`, so this app needs `OPENAI_API_KEY` even if you point the
> answer LLM at another provider via `LLM_PROVIDER`.

## The integration

```bash
diff before.py after.py
```

The only difference is `import tracesage` and wrapping the run in `with tracesage.trace():`
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global
handler captures the retriever and the LLM automatically.

## What the trace shows

- The **retriever node** as a distinct step, with its **retrieval latency** broken out from
  the LLM generation time.
- The **retrieved chunks** themselves, visible in the drawer payloads — you can read exactly
  which FAQ snippets were pulled for `k=3`.
- **Grounding**: line up the cited `[id]`s in the answer against the chunks the retriever
  returned to confirm the response is supported by context (and spot hallucinations when the
  citation doesn't match what was retrieved).
- Per-step token usage and the full stuffed prompt sent to the model.

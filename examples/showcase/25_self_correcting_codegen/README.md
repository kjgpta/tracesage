# 25 — Self-Correcting Code Generator

**Domain:** software eng · **Base:** LangGraph · **Pattern:** gen-test-fix loop

Generates a Python function for a spec, then runs it against hidden asserts in a **real
subprocess** (stdlib `subprocess`, executing a temp file). On failure the captured
traceback is fed to a fix node that revises the code, and a conditional edge loops
`generate → test → fix` until the tests pass or 3 fix attempts elapse. Self-contained:
needs only an LLM key.

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
`async with TraceLens.session(install=True)`, the `await tl.flush()` call, and a one-line
keep-the-UI-up prompt for the demo. No `callbacks=` wiring — the global handler captures
every graph node and LLM call.

## What the trace shows

- The **gen-test-fix recovery loop** as a single graph: `generate → test → fix → test`,
  with each iteration counted so you can see how many fixes a spec actually needed.
- The **test-runner tool** (`run_tests`) as a distinct node — a real subprocess writing a
  temp file and executing it — with its captured stdout/stderr in the drawer.
- **Genuine error events** when the candidate code fails its asserts: the subprocess
  traceback is surfaced as the node's output, then flows into the next LLM call.
- The **fix-on-failure path**: which iteration flipped `passed` to true, the prompt that
  carried the failing traceback into the fixer, and the conditional edge that routed back
  to `test` versus terminating at `END`.

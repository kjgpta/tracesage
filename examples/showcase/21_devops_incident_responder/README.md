# 21 — DevOps Incident Responder

**Domain:** SRE · **Base:** LangGraph · **Pattern:** tool-heavy diagnose

A prebuilt ReAct agent (`create_react_agent`) wired to three local SRE tools —
`query_logs`, `query_metrics`, and `get_recent_deploys` (all returning canned sample
data). Given an alert string, the agent loops through several tool calls to investigate,
then proposes a single `RUNBOOK:` step.

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

The only difference is `from tracesage import TraceSage` and wrapping the run in
`async with TraceSage.session(install=True)` plus an `await tl.flush()` (and a one-line
keep-the-UI-up prompt for the demo). `install=True` registers a global LangChain handler,
so there is no `callbacks=` wiring on the agent.

## What the trace shows

- The **dense tool-call sequence** — each `query_logs` / `query_metrics` /
  `get_recent_deploys` invocation as a distinct tool node under the agent.
- The **investigate-to-diagnose path**: the alternating model-step / tool-step ReAct loop
  ending at the final `RUNBOOK:` answer, so you can see how evidence led to the proposal.
- **Latency per tool** and per model step, plus token usage, so you can spot which tool
  dominated the investigation and how many turns it took to converge.

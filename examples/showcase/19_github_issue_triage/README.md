# 19 — GitHub Issue Triage

**Domain:** software eng · **Base:** LangChain · **Pattern:** tool agent

A tool-calling agent triages one hardcoded GitHub issue. It plans, then calls three
local tools in sequence — `suggest_labels`, `set_priority`, `suggest_assignee` — each
of which mutates an in-memory issue dict. No GitHub token is required; in production
these tools would be backed by the GitHub MCP server / REST API.

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
(plus a one-line keep-the-UI-up prompt for the demo). No `callbacks=` wiring — the global
handler captures the agent and every tool call automatically.

## What the trace shows

- The **multi-tool agent** loop: the LLM planning call, then the three triage tool calls
  it dispatches on a single issue.
- The **sequence of triage tool calls** (`suggest_labels` → `set_priority` →
  `suggest_assignee`), each with its arguments and return value, so you can see what the
  agent decided and in what order.
- Per-step **latency and token usage**, plus the final summary the agent produced after
  the tools returned.

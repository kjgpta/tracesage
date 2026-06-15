# 02 — Web Research ReAct Agent

**Domain:** research · **Base:** LangChain (AgentExecutor) · **Pattern:** tool-calling ReAct loop

A single agent answers a question by calling a live web-search tool (DuckDuckGo — free, no
API key), reading results, and iterating until it can answer.

## Run

```bash
pip install -r ../requirements.txt      # duckduckgo-search + langchain-community
export OPENAI_API_KEY=...
python before.py
python after.py
```

## The integration

```bash
diff before.py after.py
```

`import tracelens` + `with tracelens.trace():` around `agent.invoke(...)`.

## What the trace shows

- The **agent → tool → llm loop**: each `DuckDuckGoSearchRun` call with its query and
  returned snippets, then the model's reasoning step.
- **How many iterations** the agent took (the topology and run-trace make loops obvious).
- Token usage per LLM step — handy for spotting an agent that loops more than it should.

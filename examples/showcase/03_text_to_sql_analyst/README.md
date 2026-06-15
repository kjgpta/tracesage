# 03 — Text-to-SQL Analyst

**Domain:** data / BI · **Base:** LangChain (AgentExecutor) · **Pattern:** tool-use + error recovery

Answers natural-language questions over a seeded SQLite sales DB: the agent writes SQL,
runs it through a `run_sql` tool, and explains the result. Invalid SQL is returned as a
`SQL ERROR` string, so the agent self-corrects.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...
python before.py        # seeds a temp SQLite DB, then answers
python after.py
```

Only an LLM key is needed — the database is created locally with `sqlite3` (stdlib).

## The integration

```bash
diff before.py after.py
```

## What the trace shows

- Each **generated SQL query** as a `run_sql` tool call, with the query text and rows.
- **Error recovery**: when the model writes bad SQL, you see the `SQL ERROR` result and
  the corrected retry on the very next step — the exact loop that's invisible in logs.
- The tool node and its invocation count in the topology.

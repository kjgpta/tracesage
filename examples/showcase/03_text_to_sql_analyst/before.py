"""03 — Text-to-SQL Analyst (plain LangChain).

Answers natural-language questions about a sales database by writing SQL, running it via
a tool, and explaining the result. Pattern: tool-calling agent over a real (seeded)
SQLite DB. If the model writes invalid SQL, the tool returns the error and the agent
retries — a natural error-recovery loop.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python before.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

SCHEMA = """\
customers(id INTEGER, name TEXT, country TEXT)
orders(id INTEGER, customer_id INTEGER, amount REAL, status TEXT)"""

_DB_PATH = str(Path(tempfile.gettempdir()) / "tracelens_showcase_sales.db")


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def seed_db() -> None:
    conn = sqlite3.connect(_DB_PATH)
    conn.executescript(
        """
        DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS customers;
        CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, country TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
        INSERT INTO customers VALUES (1,'Acme','US'),(2,'Globex','DE'),(3,'Initech','US');
        INSERT INTO orders VALUES
            (1,1,1200.0,'paid'),(2,1,300.0,'paid'),(3,2,900.0,'refunded'),
            (4,2,1500.0,'paid'),(5,3,250.0,'paid'),(6,3,2000.0,'paid');
        """
    )
    conn.commit()
    conn.close()


@tool
def run_sql(query: str) -> str:
    """Execute a read-only SQL query against the sales database; return rows as JSON."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur = conn.execute(query)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        conn.close()
        return json.dumps({"columns": cols, "rows": rows[:50]}, default=str)
    except Exception as e:  # returned (not raised) so the agent can self-correct
        return f"SQL ERROR: {e}"


def build_agent() -> AgentExecutor:
    llm = make_llm()
    tools = [run_sql]
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a data analyst for a SQLite sales DB with schema:\n"
                       f"{SCHEMA}\n\nWrite SQL, run it with run_sql, and explain the answer "
                       "in one sentence. If a query errors, fix it and try again."),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False, max_iterations=5)


def main() -> None:
    seed_db()
    agent = build_agent()
    question = "Which customer has the highest total of paid orders?"
    print(f"Q: {question}\n")
    result = agent.invoke({"input": question})
    print("A:", result["output"])


if __name__ == "__main__":
    main()

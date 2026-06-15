"""19 — GitHub Issue Triage (plain LangChain).

A tool-calling agent triages one hardcoded GitHub issue using local tools that
mutate an in-memory issue dict: suggest_labels, set_priority, suggest_assignee.
Pattern: a single agent that plans and calls several tools in sequence on one
record. In production these tools would be backed by the GitHub MCP server / REST
API; here they are pure local functions so no GitHub token is needed.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import os

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

ISSUE: dict[str, object] = {
    "number": 482,
    "title": "App crashes on startup after upgrading to v2.3",
    "body": "Since updating, the dashboard throws a NullPointer on launch. Logs attached.",
    "labels": [],
    "priority": None,
    "assignee": None,
}


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


@tool
def suggest_labels(labels: list[str]) -> str:
    """Attach one or more triage labels (e.g. bug, regression, ui) to the issue."""
    ISSUE["labels"] = labels
    return f"Labels set to {labels}."


@tool
def set_priority(priority: str) -> str:
    """Set issue priority to one of: P0, P1, P2, P3 (P0 = most urgent)."""
    ISSUE["priority"] = priority
    return f"Priority set to {priority}."


@tool
def suggest_assignee(team: str) -> str:
    """Route the issue to a team: frontend, backend, infra, or docs."""
    routing = {"frontend": "alice", "backend": "bob", "infra": "carol", "docs": "dan"}
    assignee = routing.get(team.lower(), "triage-bot")
    ISSUE["assignee"] = assignee
    return f"Assigned to {assignee} ({team} team)."


def build_agent() -> AgentExecutor:
    llm = make_llm()
    tools = [suggest_labels, set_priority, suggest_assignee]
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a GitHub issue triage bot. For the given issue, call the "
                "tools to assign labels, a priority, and an owning team. Use each "
                "tool at most once, then give a one-line summary.",
            ),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, max_iterations=6, verbose=False)


def main() -> None:
    agent = build_agent()
    issue_text = f"Issue #{ISSUE['number']}: {ISSUE['title']}\n\n{ISSUE['body']}"
    print(issue_text, "\n")
    result = agent.invoke({"input": issue_text})
    print("Summary:", result["output"])
    print("Final issue state:", ISSUE)


if __name__ == "__main__":
    main()

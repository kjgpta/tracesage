"""02 — Web Research ReAct Agent (with tracesage).

Identical to before.py except for the tracesage lines. The trace makes the ReAct loop
visible: each search tool call, its query and results, and how many iterations the agent
took before answering.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python after.py
"""
from __future__ import annotations

import os
import sys

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.chat_models import init_chat_model
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

import tracesage  # ← tracesage


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_agent() -> AgentExecutor:
    llm = make_llm()
    tools = [DuckDuckGoSearchRun()]
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a research assistant. Use web search to find current, "
                       "factual information, then answer concisely with a source."),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False, max_iterations=4)


def main() -> None:
    agent = build_agent()
    question = "Who won the most recent FIFA World Cup, and in what year?"
    print(f"Q: {question}\n")

    with tracesage.trace():  # ← tracesage: starts the UI + captures the agent loop
        result = agent.invoke({"input": question})
        print("A:", result["output"])
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()

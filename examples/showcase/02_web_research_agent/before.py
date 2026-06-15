"""02 — Web Research ReAct Agent (plain LangChain).

A tool-calling agent that answers questions using live web search (DuckDuckGo, no API
key). Pattern: the classic ReAct loop — the model decides when to call the search tool,
reads results, and iterates until it can answer.

Run:
    pip install -r ../requirements.txt          # needs duckduckgo-search + langchain-community
    export OPENAI_API_KEY=...
    python before.py
"""
from __future__ import annotations

import os

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.chat_models import init_chat_model
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable


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
    result = agent.invoke({"input": question})
    print("A:", result["output"])


if __name__ == "__main__":
    main()

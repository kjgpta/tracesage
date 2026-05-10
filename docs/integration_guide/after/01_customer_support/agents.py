"""Specialist agent nodes for the customer support graph.

Each agent is a LangGraph node (an async function). It:
    1. Asks an LLM which tool to invoke for the current query.
    2. Invokes that tool.
    3. Asks an LLM to phrase the result for the customer.

This pattern is intentionally simpler than `langchain.agents.AgentExecutor`
so the trace stays readable; for production you would typically use
`langgraph.prebuilt.create_react_agent` with a tool-calling LLM.

The fake-LLM responses below are consumed in order across queries. With a
real LLM the response lists are unused — set `LLM_PROVIDER=openai` (or
`anthropic`) and an API key.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from llm import get_llm
from tools import BILLING_TOOLS, TECH_TOOLS


class SupportState(TypedDict, total=False):
    query: str
    category: str
    resolution: str
    tool_used: str
    handed_off: bool


# Pre-canned responses for the fake LLM. Each list is consumed in order
# across the demo's queries. Any list shorter than the number of invocations
# will simply cycle round-robin (FakeListChatModel behavior).
_BILLING_TOOL_PICKS = [
    "issue_refund",
    "check_balance",
    "lookup_account",
    "lookup_account",
]
_BILLING_REPLIES = [
    "Refund processed; you should see it in 3-5 days.",
    "Your current balance is up to date.",
    "Account looks healthy.",
    "Account info pulled.",
]
_TECH_TOOL_PICKS = [
    "run_diagnostic",
    "check_logs",
    "restart_service",
    "check_logs",
]
_TECH_REPLIES = [
    "Diagnostic clean - no action needed.",
    "Logs show two warnings but nothing critical.",
    "Service restarted; please retry now.",
    "Recent logs included for context.",
]
_ESCALATION_REPLIES = [
    "Forwarded to a human agent. ETA: 15 minutes.",
    "Escalated; specialist will reach out shortly.",
]

# Module-level singletons so FakeListChatModel cycles its responses across
# queries (each invocation of an agent picks the next response in line).
_billing_selector = get_llm(responses=_BILLING_TOOL_PICKS)
_billing_replier = get_llm(responses=_BILLING_REPLIES)
_tech_selector = get_llm(responses=_TECH_TOOL_PICKS)
_tech_replier = get_llm(responses=_TECH_REPLIES)
_escalation_llm = get_llm(responses=_ESCALATION_REPLIES)


async def billing_agent(state: SupportState) -> dict:
    """Billing specialist — picks one billing tool, runs it, phrases the reply."""
    return await _specialist(
        state,
        tools=BILLING_TOOLS,
        selector=_billing_selector,
        replier=_billing_replier,
    )


async def tech_agent(state: SupportState) -> dict:
    """Tech specialist — picks one tech tool, runs it, phrases the reply."""
    return await _specialist(
        state,
        tools=TECH_TOOLS,
        selector=_tech_selector,
        replier=_tech_replier,
    )


async def escalation_agent(state: SupportState) -> dict:
    """Escalation handler — no tools, just hand off to a human."""
    answer = await _escalation_llm.ainvoke(
        [HumanMessage(content=f"Escalate to human: {state['query']}")]
    )
    return {
        "resolution": answer.content,
        "tool_used": "<escalation>",
        "handed_off": True,
    }


async def _specialist(
    state: SupportState,
    *,
    tools: list[BaseTool],
    selector: Any,
    replier: Any,
) -> dict:
    tool_names = [t.name for t in tools]

    # 1. Tool-selection LLM.
    choice = await selector.ainvoke(
        [HumanMessage(content=f"Pick a tool from {tool_names} for: {state['query']}")]
    )
    chosen_name = choice.content.strip().lower()
    chosen = next((t for t in tools if t.name == chosen_name), tools[0])

    # 2. Tool invocation.
    args = _synthesize_args(chosen, state)
    tool_result = await chosen.ainvoke(args)

    # 3. Reply-formatting LLM.
    answer = await replier.ainvoke(
        [HumanMessage(content=f"Customer asked '{state['query']}'. Tool said: {tool_result}")]
    )
    return {"resolution": answer.content, "tool_used": chosen.name}


def _synthesize_args(tool: BaseTool, state: SupportState) -> dict:
    """Build a synthetic tool input from the customer's query.

    Real systems have the LLM emit structured tool calls; here we hardcode
    plausible defaults so the fake LLM has nothing to fabricate.
    """
    name = tool.name
    if name == "lookup_account":
        return {"account_id": "ACME-9001"}
    if name == "issue_refund":
        return {
            "account_id": "ACME-9001",
            "amount": 19.99,
            "reason": state["query"][:40],
        }
    if name == "check_balance":
        return {"account_id": "ACME-9001"}
    if name == "run_diagnostic":
        return {"service": "checkout-api"}
    if name == "restart_service":
        return {"service": "checkout-api"}
    if name == "check_logs":
        return {"service": "checkout-api", "lines": 100}
    return {}

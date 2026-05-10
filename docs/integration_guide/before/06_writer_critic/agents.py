"""Writer + critic agents and the cyclic-loop control flow.

The critic emits a verdict containing either `PASS` or `REVISE`. The graph
loops back to the writer on `REVISE` until either pass or a hard cap of
3 attempts. With the fake LLM, prompt 2 is pre-programmed to require a
revision; prompts 1 and 3 pass on the first try.
"""
from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import HumanMessage

from llm import get_llm
from tools import readability_check, word_count


class WriterCriticState(TypedDict, total=False):
    topic: str
    draft: str
    feedback: str
    verdict: str
    word_info: str
    readability_info: str
    attempts: int
    final: str


# Drafts the writer produces. Demo flow (3 prompts):
#   prompt 1, attempt 1 -> draft A1 (passes)
#   prompt 2, attempt 1 -> draft B1 (revise)
#   prompt 2, attempt 2 -> draft B2 (passes)
#   prompt 3, attempt 1 -> draft C1 (passes)
_WRITER_RESPONSES = [
    "Multi-agent systems split work across specialists, each with their own tools.",
    "Observability lets you see what your code does at runtime.",
    "Observability — using detailed traces — exposes runtime behavior, including agent loops and tool calls.",
    "LCEL is a fluent way to compose prompt | LLM | parser into a single chain.",
]

# Verdicts are consumed in this order:
#   PASS, REVISE: revise sentence 2 for clarity, PASS, PASS
_CRITIC_RESPONSES = [
    "PASS — concise and accurate.",
    "REVISE: too vague. Specify what observability shows.",
    "PASS — clearer now.",
    "PASS — solid LCEL summary.",
]


_writer_llm = get_llm(responses=_WRITER_RESPONSES)
_critic_llm = get_llm(responses=_CRITIC_RESPONSES)


async def writer_node(state: WriterCriticState) -> dict:
    """Generate a draft. On retry, takes the critic's feedback as guidance."""
    if state.get("attempts", 0) > 0 and state.get("feedback"):
        prompt = f"Revise considering this feedback: {state['feedback']}\nTopic: {state['topic']}"
    else:
        prompt = f"Write a 1-2 sentence summary of: {state['topic']}"
    msg = await _writer_llm.ainvoke([HumanMessage(content=prompt)])
    return {"draft": msg.content, "attempts": state.get("attempts", 0) + 1}


async def critic_node(state: WriterCriticState) -> dict:
    """Score the draft + run two ground-truth tools."""
    msg = await _critic_llm.ainvoke(
        [HumanMessage(content=f"Critique this draft: {state.get('draft', '')}")]
    )
    wc = await word_count.ainvoke({"text": state.get("draft", "")})
    rb = await readability_check.ainvoke({"text": state.get("draft", "")})
    return {
        "feedback": msg.content,
        "verdict": msg.content.split()[0].upper(),
        "word_info": wc,
        "readability_info": rb,
    }


async def finalize_node(state: WriterCriticState) -> dict:
    """Wrap the accepted draft with metadata."""
    final = (
        f"[attempts={state.get('attempts', 0)}; "
        f"{state.get('word_info', '')}; "
        f"{state.get('readability_info', '')}]\n"
        f"{state.get('draft', '')}"
    )
    return {"final": final}

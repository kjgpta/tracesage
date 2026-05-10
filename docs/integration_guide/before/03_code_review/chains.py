"""LCEL chains for code analysis and comment generation.

Each `prompt | llm | parser` pipeline becomes a `RunnableSequence` in tracelens
and decomposes into separate `chain:ChatPromptTemplate`, `llm:...`, and
`chain:StrOutputParser` topology nodes.
"""
from __future__ import annotations

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from llm import get_llm


# Pre-canned analyses, one per diff in the demo (3 diffs => 3 analyses).
_ANALYZE_RESPONSES = [
    "Diff wraps an external API call in try/except. Risk surface: error path masking.",
    "Diff renames helper variables for clarity. No behavior change.",
    "Diff lowers the default request timeout from 30s to 5s. Affects retry budget.",
]

# Comments are consumed in this order across the demo:
#   diff 1, comment attempt 1
#   diff 2, comment attempt 1 (returns RETRY -> triggers retry edge)
#   diff 2, comment attempt 2 (passes)
#   diff 3, comment attempt 1
_COMMENT_RESPONSES = [
    "LGTM. Add a comment explaining why the bare except is acceptable here.",
    "Comments unclear; needs RETRY for clarity on tested paths.",
    "Refined: variable renames are mechanical, no review concerns.",
    "Approve with note: timeout change deserves a regression test.",
]

_analyze_llm = get_llm(responses=_ANALYZE_RESPONSES)
_comment_llm = get_llm(responses=_COMMENT_RESPONSES)

_analyze_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "You are a senior code reviewer. Summarize the diff in one paragraph."),
        ("human", "{diff}"),
    ]
)
_comment_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "Generate review comments based on this analysis. Be specific."),
        ("human", "{analysis}"),
    ]
)

# Each chain is a RunnableSequence. tracelens captures each segment
# (prompt template, LLM, output parser) as its own topology node.
analyze_chain = _analyze_prompt | _analyze_llm | StrOutputParser()
comment_chain = _comment_prompt | _comment_llm | StrOutputParser()

"""LCEL chains for reranking and answer generation."""
from __future__ import annotations

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from llm import get_llm


# The reranker scores docs and emits a comma-separated list of indices to keep.
# Three queries; each produces one ranking string. The mapping below picks the
# 3 most relevant docs for each query from the 8-doc corpus.
_RERANK_RESPONSES = [
    "0,2,4",   # multi-agent systems → docs 0 (multi-agent), 2 (observability), 4 (retrievers)
    "4,2,6",   # how does retrieval work → 4 (retrievers), 2 (observability), 6 (LCEL)
    "6,0,2",   # LCEL question         → 6 (LCEL), 0 (multi-agent), 2 (observability)
]
_ANSWER_RESPONSES = [
    "Multi-agent systems delegate work across roles, monitored via observability and grounded by retrievers.",
    "Retrievers fetch relevant context which observability tracks; LCEL chains can wrap the calls.",
    "LCEL composes prompts, LLMs, and parsers; multi-agent systems and observability often pair with it.",
]


_rerank_llm = get_llm(responses=_RERANK_RESPONSES)
_answer_llm = get_llm(responses=_ANSWER_RESPONSES)

_rerank_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Rank the candidate documents by relevance to the query. "
            "Output ONLY a comma-separated list of indices, top first.",
        ),
        ("human", "Query: {query}\n\nCandidates:\n{candidates}"),
    ]
)

_answer_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "Answer the question using ONLY the provided context."),
        ("human", "Q: {question}\n\nContext:\n{context}"),
    ]
)

# LCEL pipelines. tracelens decomposes each into prompt / LLM / parser nodes.
rerank_chain = _rerank_prompt | _rerank_llm | StrOutputParser()
answer_chain = _answer_prompt | _answer_llm | StrOutputParser()

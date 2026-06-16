"""Example 3: Full-stack RAG with LCEL chain + retriever + tools.

Demonstrates EVERY tracesage component type in one example so the topology
view shows all five columns populated:

    chain   - LangGraph (top-level orchestrator) + LCEL RunnableSequence + StrOutputParser
    agent   - retrieve, synthesize, answer (the LangGraph nodes)
    retriever - FakeRetriever
    llm     - FakeListChatModel
    tool    - summarize_documents, cite_sources

Run:
    python examples/getting_started/03_rag_with_tools.py
    # Open http://localhost:7842/ui
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.callbacks import CallbackManagerForRetrieverRun  # noqa: E402
from langchain_core.documents import Document  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.output_parsers import StrOutputParser  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from langchain_core.retrievers import BaseRetriever  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracesage import TraceSage  # noqa: E402


# ---- A fake retriever that returns deterministic documents ---------------- #


class FakeRetriever(BaseRetriever):
    """A toy retriever — returns the same canned docs regardless of query.

    For real use, plug in any LangChain retriever (FAISS, Chroma, etc.).
    """

    docs_by_topic: dict[str, list[str]]
    default_docs: list[str]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        key = next((k for k in self.docs_by_topic if k in query.lower()), None)
        contents = self.docs_by_topic.get(key, self.default_docs) if key else self.default_docs
        return [Document(page_content=c, metadata={"source": f"doc-{i}"}) for i, c in enumerate(contents)]


# ---- Post-processing tools ----------------------------------------------- #


@tool
def summarize_documents(text: str) -> str:
    """Compress retrieved documents into a short summary."""
    return f"Summary({len(text)} chars): {text[:60]}..."


@tool
def cite_sources(answer: str) -> str:
    """Append source citations to an answer."""
    return f"{answer}\n\n[1] doc-0  [2] doc-1"


class RAGState(TypedDict):
    question: str
    docs: list[str]
    summary: str
    answer: str
    cited: str


async def main() -> None:
    tracer = await TraceSage.create()
    print("tracesage at http://localhost:7842/ui")

    retriever = FakeRetriever(
        docs_by_topic={
            "agent": [
                "An agent is an LLM that uses tools.",
                "Multi-agent systems split a task across roles.",
            ],
            "retriever": [
                "Retrievers fetch context for grounding.",
                "Vector stores embed and retrieve.",
            ],
            "trace": [
                "Tracing captures execution events.",
                "Observability matters in production.",
            ],
        },
        default_docs=["No relevant docs found."],
    )

    synth_llm = FakeListChatModel(
        responses=[
            "Agents are LLMs that orchestrate tools.",
            "Retrievers fetch context to ground answers.",
            "Tracing captures every callback event.",
            "Topic not covered.",
        ]
    )
    answer_llm = FakeListChatModel(
        responses=[
            "Final: agents wield tools.",
            "Final: retrievers ground answers.",
            "Final: tracing is observability.",
            "Final: not covered.",
        ]
    )

    async def retrieve_node(state: RAGState) -> dict:
        # retriever.ainvoke fires on_retriever_start / on_retriever_end.
        docs = await retriever.ainvoke(state["question"])
        return {"docs": [d.page_content for d in docs]}

    async def synthesize_node(state: RAGState) -> dict:
        # LLM call + summarize tool.
        joined = " ".join(state["docs"])
        await synth_llm.ainvoke([HumanMessage(content=joined)])
        summary = await summarize_documents.ainvoke({"text": joined})
        return {"summary": summary}

    # An LCEL chain: prompt | llm | parser. This is a RunnableSequence; each
    # step fires its own chain_start so the topology will show CHAIN nodes.
    answer_prompt = ChatPromptTemplate.from_messages(
        [("user", "Q: {question}\nContext: {summary}")]
    )
    answer_chain = answer_prompt | answer_llm | StrOutputParser()

    async def answer_node(state: RAGState) -> dict:
        # The LCEL chain handles prompt formatting + LLM call + parsing in one go.
        text = await answer_chain.ainvoke(
            {"question": state["question"], "summary": state["summary"]}
        )
        cited = await cite_sources.ainvoke({"answer": text})
        return {"answer": text, "cited": cited}

    workflow: StateGraph = StateGraph(RAGState)
    workflow.add_node("retrieve", retrieve_node)
    workflow.add_node("synthesize", synthesize_node)
    workflow.add_node("answer", answer_node)
    workflow.set_entry_point("retrieve")
    workflow.add_edge("retrieve", "synthesize")
    workflow.add_edge("synthesize", "answer")
    workflow.add_edge("answer", END)
    graph = workflow.compile()

    questions = [
        "what is an agent?",
        "how do retrievers work?",
        "why care about trace?",
        "what is quantum entanglement?",  # falls back to default docs
    ]
    for q in questions:
        result = await graph.ainvoke(
            {"question": q, "docs": [], "summary": "", "answer": "", "cited": ""},
            config={"callbacks": [tracer.handler], "tags": ["rag"]},
        )
        print(f"  Q: {q}")
        print(f"     A: {result.get('cited', '').splitlines()[0]}")

    print("\nLeaving server up. The topology should now show:")
    print("  retriever:FakeRetriever -> llm:FakeListChatModel -> tool:summarize_documents")
    print("  -> llm:FakeListChatModel -> tool:cite_sources")
    print("Open http://localhost:7842/ui to inspect. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())

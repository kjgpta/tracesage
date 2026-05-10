"""LangGraph node implementations for the research pipeline.

The pipeline:
    ingest → retrieve → ( fact_extractor || sentiment || entities ) → synthesize

The three middle nodes run **in parallel**. They write to disjoint state
fields (`facts`, `sentiment`, `entities`) so LangGraph's default state
merging handles the fan-in cleanly.
"""
from __future__ import annotations

from typing import TypedDict

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from langchain_core.retrievers import BaseRetriever

from llm import get_llm
from tools import cite_sources, fetch_document, web_search


class ResearchState(TypedDict, total=False):
    topic: str
    raw_corpus: str          # set by ingest
    retrieved: list[str]     # set by retrieve
    facts: list[str]         # set by fact_extractor (parallel)
    sentiment: str           # set by sentiment (parallel)
    entities: list[str]      # set by entities (parallel)
    summary: str             # set by synthesize


class _FixedCorpusRetriever(BaseRetriever):
    """A toy retriever returning a fixed list of documents.

    Real systems plug in FAISS, Chroma, etc.; the integration with tracelens
    is identical regardless of retriever — anything inheriting from
    `BaseRetriever` fires the standard retriever callbacks.
    """

    documents: list[str]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        return [
            Document(page_content=d, metadata={"source": f"doc-{i}"})
            for i, d in enumerate(self.documents)
        ]


_DEFAULT_DOCS = [
    "Multi-agent systems combine specialized roles to solve complex tasks.",
    "Observability provides visibility into running systems for debugging and ops.",
    "LLM agents can call tools, retrieve context, and reason in iterative loops.",
]


# Module-level singletons so the fake LLM cycles its responses across topics.
# With a real LLM these instances are reused; the `responses` lists are unused.
_ingest_llm = get_llm(
    responses=[
        "search for sources on the topic",
        "search recent papers on the topic",
        "look up the topic in cached corpora",
    ]
)
_facts_llm = get_llm(
    responses=[
        "claim 1; claim 2; claim 3",
        "fact A; fact B",
        "key point 1; key point 2; key point 3",
    ]
)
_sentiment_llm = get_llm(responses=["positive", "neutral", "mixed"])
_entities_llm = get_llm(
    responses=[
        "agent, tool, framework",
        "system, observability, debugging",
        "LLM, retriever, reasoning",
    ]
)
_synthesize_llm = get_llm(
    responses=[
        "Synthesis: facts confirm a positive view across the named entities.",
        "Synthesis: mixed signals; entities are widely discussed.",
        "Synthesis: neutral; further research warranted.",
    ]
)


async def ingest(state: ResearchState) -> dict:
    """Plan the search and fetch one document. Two tool calls per topic."""
    await _ingest_llm.ainvoke(
        [HumanMessage(content=f"Plan how to research: {state['topic']}")]
    )
    urls = await web_search.ainvoke({"query": state["topic"], "max_results": 3})
    raw = await fetch_document.ainvoke({"url": urls.split("\n")[0]})
    return {"raw_corpus": raw}


async def retrieve(state: ResearchState) -> dict:
    """Retrieve relevant context from the fixed corpus."""
    retriever = _FixedCorpusRetriever(documents=_DEFAULT_DOCS)
    docs = await retriever.ainvoke(state["topic"])
    return {"retrieved": [d.page_content for d in docs]}


async def fact_extractor(state: ResearchState) -> dict:
    """Parallel branch — extract concrete claims from the retrieved context."""
    text = " ".join(state.get("retrieved") or [])
    response = await _facts_llm.ainvoke([HumanMessage(content=f"Extract facts: {text}")])
    return {"facts": [c.strip() for c in response.content.split(";")]}


async def sentiment(state: ResearchState) -> dict:
    """Parallel branch — overall tone of the retrieved context."""
    text = " ".join(state.get("retrieved") or [])
    response = await _sentiment_llm.ainvoke(
        [HumanMessage(content=f"Sentiment of: {text}")]
    )
    return {"sentiment": response.content.strip()}


async def entities(state: ResearchState) -> dict:
    """Parallel branch — named entities in the retrieved context."""
    text = " ".join(state.get("retrieved") or [])
    response = await _entities_llm.ainvoke(
        [HumanMessage(content=f"Entities in: {text}")]
    )
    return {"entities": [e.strip() for e in response.content.split(",")]}


async def synthesize(state: ResearchState) -> dict:
    """Merge node — combine the three parallel results into a final summary."""
    ctx = (
        f"facts={state.get('facts', [])} "
        f"sentiment={state.get('sentiment', 'n/a')} "
        f"entities={state.get('entities', [])}"
    )
    response = await _synthesize_llm.ainvoke([HumanMessage(content=ctx)])
    cited = await cite_sources.ainvoke({"answer": response.content})
    return {"summary": cited}

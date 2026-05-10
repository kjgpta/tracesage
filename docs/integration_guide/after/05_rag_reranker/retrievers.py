"""Fast first-stage retriever returning many candidates for reranking."""
from __future__ import annotations

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever


class FastFakeRetriever(BaseRetriever):
    """A toy first-stage retriever — returns 8 candidate docs per query.

    Real systems plug in FAISS / Chroma / Elastic / etc. The contract with
    tracelens is identical — every `BaseRetriever` subclass automatically
    fires `retriever_start` / `retriever_end` callbacks.
    """

    documents: list[str]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        del query, run_manager
        return [
            Document(
                page_content=d,
                metadata={"source": f"doc-{i}", "score": round(1.0 - i * 0.07, 2)},
            )
            for i, d in enumerate(self.documents)
        ]


CORPUS = [
    "Multi-agent systems route work between specialists.",
    "Cats sleep up to 16 hours a day.",
    "Observability captures execution events for debugging.",
    "The Eiffel Tower is 330 meters tall.",
    "Retrievers fetch context for grounding answers.",
    "Sourdough bread requires a starter culture.",
    "LCEL composes prompt, LLM, and parser into a chain.",
    "The mitochondria is the powerhouse of the cell.",
]

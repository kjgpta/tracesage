"""LCEL streaming chain.

The chain is `prompt | llm | StrOutputParser`. When `astream()` is called on
the chain, each LLM token chunk is parsed to a string and yielded.

For real LLMs with `streaming=True`, this yields many small chunks (one per
generated token). For `FakeListChatModel`, the response is yielded as a
single chunk, but `on_llm_new_token` still fires — tracelens captures the
streaming telemetry either way.
"""
from __future__ import annotations

import os
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from llm import get_llm


def _build_streaming_llm() -> Any:
    """Build a chat model with streaming enabled when supported by the provider."""
    provider = os.getenv("LLM_PROVIDER", "fake").lower()
    if provider == "fake":
        # FakeListChatModel emits a single chunk; that's enough to demonstrate
        # the on_llm_new_token callback path.
        return get_llm(
            responses=[
                "Yes, multi-agent systems split work across roles for scale.",
                "Observability captures every callback to make debugging tractable.",
                "LCEL chains compose primitives via the pipe operator.",
            ]
        )
    # Real providers — pass streaming=True via the model kwarg path. The
    # provider switch in llm.py constructs the model; we then enable
    # streaming via attribute mutation, which is cleaner than threading the
    # flag through get_llm()'s signature.
    model = get_llm()
    if hasattr(model, "streaming"):
        model.streaming = True  # type: ignore[attr-defined]
    return model


_streaming_llm = _build_streaming_llm()

_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "Answer the question concisely. One paragraph."),
        ("human", "{question}"),
    ]
)

streaming_chain = _prompt | _streaming_llm | StrOutputParser()

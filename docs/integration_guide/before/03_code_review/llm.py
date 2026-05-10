"""LLM provider switch for the customer support system.

Set `LLM_PROVIDER`:
    fake (default) — FakeListChatModel, zero setup, deterministic
    openai         — ChatOpenAI, requires OPENAI_API_KEY
    anthropic      — ChatAnthropic, requires ANTHROPIC_API_KEY

The integration code that calls `get_llm()` does not change between providers;
only the returned object differs.
"""
from __future__ import annotations

import os
from typing import Any


def get_llm(*, responses: list[str] | None = None, model: str | None = None) -> Any:
    """Build a chat model. `responses` is consulted only by the fake provider."""
    provider = os.getenv("LLM_PROVIDER", "fake").lower()

    if provider == "fake":
        try:
            from langchain_core.language_models.fake_chat_models import FakeListChatModel
        except ImportError:
            from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]
        return FakeListChatModel(responses=responses or ["ok"])

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model or os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        )

    raise ValueError(f"unknown LLM_PROVIDER: {provider!r}")

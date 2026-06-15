"""05 — Content Safety Pipeline (plain LangChain).

Runs three independent classifiers — toxicity, PII, and policy — concurrently over a
piece of user content, then aggregates a single allow/block verdict. Pattern: parallel
fan-out with RunnableParallel (the checks run at the same time, not one after another).

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python before.py
"""
from __future__ import annotations

import os

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda, RunnableParallel


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def _check(llm: Runnable, what: str) -> Runnable:
    return (
        ChatPromptTemplate.from_template(
            f"Does the following content contain {what}? Answer strictly 'YES' or 'NO', "
            "then a short reason on the same line.\n\nContent: {content}"
        )
        | llm
        | StrOutputParser()
    )


def _aggregate(checks: dict) -> dict:
    flagged = [name for name, verdict in checks.items() if verdict.strip().upper().startswith("YES")]
    return {"decision": "BLOCK" if flagged else "ALLOW", "flagged_by": flagged, "checks": checks}


def build_chain() -> Runnable:
    llm = make_llm()
    checks = RunnableParallel(
        toxicity=_check(llm, "toxic, harassing, or hateful language"),
        pii=_check(llm, "personal identifiable information (emails, phone numbers, SSNs)"),
        policy=_check(llm, "requests for illegal activity or self-harm"),
    )
    return checks | RunnableLambda(_aggregate)


def main() -> None:
    chain = build_chain()
    content = "Hey, email me at jane.doe@example.com and I'll send the spreadsheet."
    result = chain.invoke({"content": content})
    print("DECISION:", result["decision"], "| flagged by:", result["flagged_by"])


if __name__ == "__main__":
    main()

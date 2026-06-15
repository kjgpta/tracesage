"""25 — Self-Correcting Code Generator (plain LangGraph).

Generates a Python function for a spec, then a run_tests node writes the code plus
hidden asserts to a temp file and executes it with a real subprocess, capturing
pass/fail. On failure a fix node revises the code with the captured traceback. A
conditional edge loops generate→test→fix until the tests pass or 3 fixes elapse.
Pattern: gen-test-fix self-correction loop with real subprocess execution.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

MAX_FIXES = 3
SPEC = "Write `def is_palindrome(s: str) -> bool` ignoring case and non-alphanumerics."
TESTS = (
    "assert is_palindrome('A man, a plan, a canal: Panama')\n"
    "assert not is_palindrome('hello')\n"
    "assert is_palindrome('')\n"
)


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def run_tests(code: str) -> tuple[bool, str]:
    """Write code + asserts to a temp file and run it; return (passed, output)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "candidate.py"
        path.write_text(code + "\n\n" + TESTS, encoding="utf-8")
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


class CodeState(TypedDict):
    spec: str
    code: str
    error: str
    passed: bool
    fixes: int


def build_graph() -> Runnable:
    llm = make_llm()
    generate = (
        ChatPromptTemplate.from_template(
            "Write ONLY the Python code (no prose, no fences) for this spec.\n\n{spec}"
        )
        | llm
        | StrOutputParser()
    )
    fixer = (
        ChatPromptTemplate.from_template(
            "This Python code failed its tests. Return ONLY the corrected code.\n\n"
            "Spec: {spec}\n\nCode:\n{code}\n\nError:\n{error}"
        )
        | llm
        | StrOutputParser()
    )

    async def generate_node(state: CodeState) -> dict:
        code = _strip_fences(await generate.ainvoke({"spec": state["spec"]}))
        return {"code": code}

    async def test_node(state: CodeState) -> dict:
        passed, output = await asyncio.to_thread(run_tests, state["code"])
        return {"passed": passed, "error": "" if passed else output}

    async def fix_node(state: CodeState) -> dict:
        code = _strip_fences(
            await fixer.ainvoke(
                {"spec": state["spec"], "code": state["code"], "error": state["error"]}
            )
        )
        return {"code": code, "fixes": state["fixes"] + 1}

    def route(state: CodeState) -> str:
        if state["passed"] or state["fixes"] >= MAX_FIXES:
            return "done"
        return "fix"

    builder = StateGraph(CodeState)
    builder.add_node("generate", generate_node)
    builder.add_node("test", test_node)
    builder.add_node("fix", fix_node)
    builder.add_edge(START, "generate")
    builder.add_edge("generate", "test")
    builder.add_conditional_edges("test", route, {"fix": "fix", "done": END})
    builder.add_edge("fix", "test")
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    print(f"Spec: {SPEC}\n")
    result = await graph.ainvoke(
        {"spec": SPEC, "code": "", "error": "", "passed": False, "fixes": 0}
    )
    print(f"Passed: {result['passed']} ({result['fixes']} fix attempts)\n")
    print(result["code"])


if __name__ == "__main__":
    asyncio.run(main())

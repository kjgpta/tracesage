"""28 — Invoice / Expense Extractor (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows each structured-output extraction call and the parsed
Invoice it produced, so you can see exactly what the model returned before the PASS/FAIL
validation runs on top of it.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python after.py
"""
from __future__ import annotations

import os
import sys

from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

import tracelens  # ← tracelens


class LineItem(BaseModel):
    description: str = Field(description="What was billed")
    amount: float = Field(description="Line amount in dollars")


class Invoice(BaseModel):
    vendor: str = Field(description="Name of the company that issued the invoice")
    date: str = Field(description="Invoice date as written, e.g. 2026-05-01")
    line_items: list[LineItem] = Field(description="Itemized charges")
    total: float = Field(description="Stated grand total in dollars")


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_extractor() -> Runnable:
    llm = make_llm()
    prompt = ChatPromptTemplate.from_template(
        "Extract the invoice fields from the text below. Use the stated total exactly "
        "as written — do not recompute it.\n\nINVOICE:\n{text}"
    )
    return prompt | llm.with_structured_output(Invoice)


def validate(inv: Invoice, tolerance: float = 0.01) -> bool:
    line_sum = round(sum(item.amount for item in inv.line_items), 2)
    return abs(line_sum - inv.total) <= tolerance


def report(label: str, extractor: Runnable, text: str) -> None:
    inv: Invoice = extractor.invoke({"text": text})
    line_sum = round(sum(item.amount for item in inv.line_items), 2)
    ok = validate(inv)
    print(f"\n=== {label} ===")
    print(f"Vendor: {inv.vendor}  Date: {inv.date}")
    for item in inv.line_items:
        print(f"  - {item.description}: {item.amount:.2f}")
    print(f"Line-item sum: {line_sum:.2f}  Stated total: {inv.total:.2f}")
    print("VALIDATION:", "PASS ✅" if ok else "FAIL ❌")


# A clean invoice whose line items add up to the stated total.
GOOD_INVOICE = """
Acme Cloud Services — Invoice #4471, dated 2026-05-01
  Compute (May)      $120.00
  Object storage      $35.50
  Support add-on      $44.50
Total due: $200.00
"""

# A messy invoice where the stated total does NOT match the line items.
BAD_INVOICE = """
From: Bright Office Supplies   2026/05/14
qty 2 ergonomic chairs ......... 480.00
1x standing desk ............... 310.00
shipping ....................... 25.00
GRAND TOTAL ................... 900.00
"""


def main() -> None:
    extractor = build_extractor()
    with tracelens.trace():  # ← tracelens: starts the UI + captures every call
        report("Clean invoice (should PASS)", extractor, GOOD_INVOICE)
        report("Messy invoice (should FAIL)", extractor, BAD_INVOICE)
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()

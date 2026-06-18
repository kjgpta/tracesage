"""22 — E-commerce Shopping Concierge (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open
the printed link: the trace shows each agent step and every action tool call —
search_catalog, add_to_cart (a side-effecting cart mutation), and view_cart — so you can
see exactly what the agent did to the cart and why.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python after.py
"""
from __future__ import annotations

import os
import sys

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_core.tools import tool

from pathlib import Path  # ← tracesage
import tracesage  # ← tracesage

# tracesage: dedicated per-demo data dir so this app's runs, topology, and
# "Tools by source" stay isolated from other demos (each app = its own dir).
DATA_DIR = Path.home() / ".tracesage" / Path(__file__).resolve().parent.name


CATALOG = [
    {"sku": "TS-01", "name": "Cotton T-Shirt", "price": 19.0, "tags": "shirt top casual"},
    {"sku": "JK-02", "name": "Denim Jacket", "price": 89.0, "tags": "jacket outerwear denim"},
    {"sku": "SN-03", "name": "Running Sneakers", "price": 120.0, "tags": "shoes sneakers sport"},
    {"sku": "CP-04", "name": "Baseball Cap", "price": 25.0, "tags": "hat cap accessory"},
]
CART: dict[str, int] = {}


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


@tool
def search_catalog(query: str) -> str:
    """Search the product catalog by keyword; returns matching SKUs, names, and prices."""
    q = query.lower()
    hits = [p for p in CATALOG if q in p["name"].lower() or q in p["tags"]]
    if not hits:
        return "No products matched."
    return "\n".join(f"{p['sku']}: {p['name']} (${p['price']:.2f})" for p in hits)


@tool
def add_to_cart(sku: str, quantity: int = 1) -> str:
    """Add a quantity of a product (by SKU) to the shopping cart."""
    if not any(p["sku"] == sku for p in CATALOG):
        return f"Unknown SKU {sku!r}."
    CART[sku] = CART.get(sku, 0) + quantity
    return f"Added {quantity} x {sku}. Cart now holds {CART[sku]} of {sku}."


@tool
def view_cart() -> str:
    """Show the current contents and total price of the shopping cart."""
    if not CART:
        return "Cart is empty."
    lines, total = [], 0.0
    for sku, qty in CART.items():
        p = next(x for x in CATALOG if x["sku"] == sku)
        total += p["price"] * qty
        lines.append(f"{qty} x {p['name']} ({sku}) = ${p['price'] * qty:.2f}")
    return "\n".join(lines) + f"\nTOTAL: ${total:.2f}"


def build_agent() -> AgentExecutor:
    llm = make_llm()
    tools = [search_catalog, add_to_cart, view_cart]
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a shopping concierge. Use the tools to find products, "
                       "add them to the cart, and confirm the cart before finishing."),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, max_iterations=6, verbose=False)


def main() -> None:
    executor = build_agent()
    request = "Find a casual shirt and a cap, add one of each to my cart, then show the cart."
    print(f"Shopper: {request}\n")

    with tracesage.trace(tracesage.TraceSageConfig(data_dir=DATA_DIR)):  # ← tracesage: starts the UI + captures every call
        result = executor.invoke({"input": request})
        print("Concierge:", result["output"])
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()

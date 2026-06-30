"""Knowledge-base MCP server (stdio transport) for the support-assistant demo.

Two tools: search_help_center and get_policy.
Data is realistic-looking but fully hardcoded — no external API calls.
Run indirectly by after.py / before.py via MultiServerMCPClient.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kb")

_ARTICLES: dict[str, str] = {
    "delivery": (
        "Help article — Tracking your delivery:\n"
        "  Once an order ships you get a tracking link by email. Standard delivery\n"
        "  is 3-5 business days. You can also track from Your Orders -> Track."
    ),
    "returns": (
        "Help article — Returns & refunds:\n"
        "  Items can be returned within 30 days for a full refund. Start a return\n"
        "  from Your Orders → Return item; we email a prepaid label."
    ),
}


@mcp.tool()
def search_help_center(query: str) -> str:
    """Search the help center for an article matching the customer's question."""
    q = query.lower()
    if any(w in q for w in ("deliver", "ship", "track", "where", "arrive")):
        return _ARTICLES["delivery"]
    if any(w in q for w in ("return", "refund", "money back")):
        return _ARTICLES["returns"]
    return "No exact help article found. Suggest contacting support for specifics."


@mcp.tool()
def get_policy(topic: str) -> str:
    """Get the official company policy for a topic (e.g. 'shipping', 'returns')."""
    t = topic.lower()
    if "ship" in t or "deliver" in t:
        return (
            "Shipping policy: Free standard shipping on orders over $50. "
            "Delivery in 3-5 business days. Delayed orders are eligible for a "
            "$10 credit on request."
        )
    if "return" in t or "refund" in t:
        return (
            "Returns policy: 30-day window, free return shipping, refunds issued "
            "to the original payment method within 5 business days of receipt."
        )
    return f"No policy on file for '{topic}'."


if __name__ == "__main__":
    mcp.run(transport="stdio")

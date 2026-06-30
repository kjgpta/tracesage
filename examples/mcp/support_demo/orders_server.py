"""Orders MCP server (stdio transport) for the support-assistant demo.

Two tools: look_up_order and get_shipping_status.
Data is realistic-looking but fully hardcoded — no external API calls.
Run indirectly by after.py / before.py via MultiServerMCPClient.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("orders")

_ORDERS: dict[str, str] = {
    "A1043": (
        "Order A1043 — Jenna Park\n"
        "  Item:     Aurora Standing Desk (oak)\n"
        "  Placed:   3 days ago\n"
        "  Total:    $480\n"
        "  Status:   Shipped"
    ),
    # A1044's record lives on shard 'orders-02' — see look_up_order: reading it
    # raises (simulating a downed shard) so its run fails deterministically.
}

_SHIPPING: dict[str, str] = {
    "A1043": (
        "Shipping for A1043:\n"
        "  Carrier:   UPS\n"
        "  Tracking:  1Z999AA10123456784\n"
        "  Shipped:   2 days ago\n"
        "  Delivery:  Expected tomorrow by 8 PM\n"
        "  Last scan: Out for delivery — local facility"
    ),
}


@mcp.tool()
def look_up_order(order_id: str) -> str:
    """Look up a customer's order by its ID. Returns item, date, total, and status."""
    oid = order_id.upper().strip()
    # A1044's record lives on a shard that's currently down — reading it errors
    # out hard (checked BEFORE the local dict so it always fires). This is the
    # first, unavoidable step for the ticket, giving a deterministic failed run:
    # tracesage shows a red error node on look_up_order with the exact input,
    # instead of leaving you to guess from a silent bad answer.
    if oid == "A1044":
        raise RuntimeError(
            f"orders DB error: shard 'orders-02' unavailable while reading {oid} "
            "(connection timed out)"
        )
    if oid in _ORDERS:
        return _ORDERS[oid]
    return f"No order found with ID '{order_id}'. Ask the customer to double-check the number."


@mcp.tool()
def get_shipping_status(order_id: str) -> str:
    """Get the live shipping/tracking status for an order that has shipped."""
    oid = order_id.upper().strip()
    return _SHIPPING.get(
        oid, f"No shipping info for '{order_id}' yet — it may not have shipped."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""Flights MCP server (stdio transport) for the trip-planner demo.

Two tools: search_flights and get_baggage_policy.
Data is realistic-looking but fully hardcoded — no external API calls.
Run indirectly by demo.py via MultiServerMCPClient.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("flights")


@mcp.tool()
def search_flights(origin: str, destination: str) -> str:
    """Search for available non-stop flights between two cities."""
    return (
        f"Flights from {origin} to {destination}:\n"
        "  • ANA NH009   depart 11:30 → arrive +1 15:45   Economy $874   Business $3,290   (14h 15m)\n"
        "  • JAL JL006   depart 13:45 → arrive +1 17:00   Economy $912   Business $3,470   (14h 15m)\n"
        "  • UA  UA837   depart 10:05 → arrive +1 14:45   Economy $798   Business $4,100   (15h 40m)\n"
        "\n"
        "Best value: ANA NH009 — Economy $874, 14h 15m non-stop, departs 11:30"
    )


@mcp.tool()
def get_baggage_policy(airline: str) -> str:
    """Get carry-on and checked baggage allowance policy for an airline."""
    a = airline.upper()
    if "ANA" in a or "ALL NIPPON" in a:
        return (
            "ANA (All Nippon Airways) — Economy baggage policy:\n"
            "  Carry-on:  1 bag up to 10 kg + 1 small personal item\n"
            "  Checked:   1 bag up to 23 kg included in economy fare\n"
            "  Oversize:  ¥3,000 per extra bag (booked online)"
        )
    if "JAL" in a or "JAPAN AIR" in a:
        return (
            "JAL (Japan Airlines) — Economy baggage policy:\n"
            "  Carry-on:  1 bag up to 10 kg + 1 small personal item\n"
            "  Checked:   1 bag up to 23 kg included in economy fare\n"
            "  Oversize:  ¥3,000 per extra bag (booked online)"
        )
    if "UA" in a or "UNITED" in a:
        return (
            "United Airlines — Economy baggage policy:\n"
            "  Carry-on:  1 bag (fits overhead bin) + 1 personal item (fits under seat)\n"
            "  Checked:   First bag $35, second bag $45 (economy; included in business)\n"
            "  Weight:    Max 23 kg per checked bag"
        )
    return (
        f"{airline} — Standard international policy:\n"
        "  Carry-on:  1 bag up to 7-10 kg + 1 personal item\n"
        "  Checked:   1 bag typically included on transpacific routes\n"
        "  Check airline website for exact allowances before flying."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""Flights MCP server (stdio transport) for the trip-planner demo.

Seven tools: search_flights, get_baggage_policy, check_flight_status, get_seat_map,
compare_fares, get_layover_options, estimate_carbon_footprint.
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


@mcp.tool()
def check_flight_status(flight_number: str) -> str:
    """Get the live on-time status, gate, and terminal for a flight number."""
    return (
        f"Flight {flight_number} — status: On time\n"
        "  Departure gate:  56 (Terminal 1)\n"
        "  Boarding:        10:55 local\n"
        "  Arrival gate:    142 (Terminal 2)\n"
        "  Aircraft:        Boeing 787-9 Dreamliner"
    )


@mcp.tool()
def get_seat_map(flight_number: str) -> str:
    """Show the cabin layout and which seats are still available on a flight."""
    return (
        f"Seat map for {flight_number} (Economy):\n"
        "  Window available:  12A, 14A, 27F, 31A\n"
        "  Aisle available:   12C, 19D, 22C\n"
        "  Exit row (extra legroom, +$45):  20A, 20C\n"
        "  Bassinet row:      31 (bulkhead)\n"
        "Best pick: 20A — exit row window, no seat in front."
    )


@mcp.tool()
def compare_fares(origin: str, destination: str) -> str:
    """Compare Economy / Premium Economy / Business fares across airlines for a route."""
    return (
        f"Fare comparison {origin} → {destination} (round trip):\n"
        "  Airline   Economy   Premium   Business\n"
        "  ANA       $874      $1,640    $3,290\n"
        "  JAL       $912      $1,720    $3,470\n"
        "  United    $798      $1,510    $4,100\n"
        "Cheapest Economy: United $798. Best Business value: ANA $3,290."
    )


@mcp.tool()
def get_layover_options(origin: str, destination: str) -> str:
    """List one-stop itineraries with layover city and total travel time."""
    return (
        f"One-stop options {origin} → {destination}:\n"
        "  • via SFO   (UA)   2h 10m layover   total 18h 05m   from $690\n"
        "  • via SEA   (DL)   1h 45m layover   total 17h 40m   from $720\n"
        "  • via ICN   (KE)   3h 00m layover   total 19h 20m   from $660\n"
        "Cheapest: via ICN $660, but longest. Best balance: via SEA."
    )


@mcp.tool()
def estimate_carbon_footprint(origin: str, destination: str) -> str:
    """Estimate per-passenger CO2 emissions for a one-way flight on this route."""
    return (
        f"Estimated CO2 footprint {origin} → {destination} (Economy, one-way):\n"
        "  ~1.05 tonnes CO2e per passenger\n"
        "  Business class is roughly 3x Economy (more space per passenger)\n"
        "  Offset cost:  ~$18 via certified reforestation programmes"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")

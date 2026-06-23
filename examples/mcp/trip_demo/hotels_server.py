"""Hotels MCP server (stdio transport) for the trip-planner demo.

Two tools: search_hotels and get_hotel_details.
Data is realistic-looking but fully hardcoded — no external API calls.
Run indirectly by demo.py via MultiServerMCPClient.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hotels")

_SEARCH: dict[str, str] = {
    "tokyo": (
        "Top hotels in Tokyo:\n"
        "  ★★★★★  Park Hyatt Tokyo         Shinjuku     $420/night   iconic city views, jazz bar\n"
        "  ★★★★★  Aman Tokyo               Otemachi     $1,100/night minimalist luxury, urban spa\n"
        "  ★★★★   Cerulean Tower Tokyu     Shibuya      $220/night   best transit links, rooftop bar\n"
        "  ★★★★   Shinjuku Granbell Hotel  Shinjuku     $165/night   stylish boutique, nightlife access\n"
        "\n"
        "Best value pick: Cerulean Tower Tokyu — central Shibuya, JR + subway 2-min walk."
    ),
    "london": (
        "Top hotels in London:\n"
        "  ★★★★★  The Savoy                Strand       £580/night   historic luxury, Thames views\n"
        "  ★★★★★  Claridge's               Mayfair      £650/night   art deco icon, Michelin dining\n"
        "  ★★★★   The Hoxton Shoreditch    East London  £180/night   trendy boutique, great café\n"
        "  ★★★★   citizenM Tower of London City         £160/night   compact smart rooms, great location\n"
        "\n"
        "Best value pick: citizenM Tower of London — near Tower Bridge, fast check-in, all-inclusive WiFi."
    ),
}

_DETAILS: dict[str, str] = {
    "cerulean tower tokyu": (
        "Cerulean Tower Tokyu Hotel — Shibuya, Tokyo\n"
        "  Rooms:      411 rooms (Deluxe to Penthouse Suite), floors 19-40\n"
        "  Amenities:  Free WiFi, rooftop bar (floor 40), Japanese garden, 2 restaurants\n"
        "              Noh theatre, banquet facilities\n"
        "  Check-in:   15:00  /  Check-out: 12:00\n"
        "  Transport:  2-min walk to Shibuya Station (JR Yamanote line + Ginza/Hanzomon subway)\n"
        "  Note:       Excellent base for Harajuku, Shinjuku, and Roppongi day trips"
    ),
    "park hyatt tokyo": (
        "Park Hyatt Tokyo — Shinjuku, Tokyo\n"
        "  Rooms:      177 rooms (all with floor-to-ceiling city views, floors 39-52)\n"
        "  Amenities:  Indoor pool, Peak Lounge, New York Bar & Grill (live jazz nightly)\n"
        "              Club On The Park fitness centre, spa treatments\n"
        "  Check-in:   15:00  /  Check-out: 12:00\n"
        "  Transport:  10-min walk to Shinjuku Station or 5-min taxi\n"
        "  Note:       Featured in 'Lost in Translation'; one of Tokyo's most iconic hotel bars"
    ),
    "aman tokyo": (
        "Aman Tokyo — Otemachi, Tokyo\n"
        "  Rooms:      84 suites (avg 107 sqm, minimalist Japanese design, highest ceilings in Tokyo)\n"
        "  Amenities:  Urban spa with 25m indoor pool, hammam, Arva Italian restaurant\n"
        "              Private car fleet, in-room butler service\n"
        "  Check-in:   15:00  /  Check-out: 12:00\n"
        "  Transport:  Direct underground access to Otemachi Station (5 subway lines)\n"
        "  Note:       Preferred by celebrities and heads of state; exceptional privacy"
    ),
    "shinjuku granbell hotel": (
        "Shinjuku Granbell Hotel — Shinjuku, Tokyo\n"
        "  Rooms:      74 rooms (boutique, contemporary art installations throughout)\n"
        "  Amenities:  Free WiFi, rooftop terrace bar (seasonal), curated art collection\n"
        "  Check-in:   15:00  /  Check-out: 11:00\n"
        "  Transport:  5-min walk to Shinjuku-Sanchome Station\n"
        "  Note:       Strong design identity; popular with creative travellers"
    ),
}


@mcp.tool()
def search_hotels(city: str, nights: int = 2) -> str:
    """Search for top-rated hotels in a city. Returns options with price per night and highlights."""
    base = _SEARCH.get(city.lower())
    if base:
        return f"(Showing options for {nights}-night stay)\n\n" + base
    return (
        f"Top hotels in {city} (for {nights} nights):\n"
        "  ★★★★★  Grand Luxury Hotel    City Centre   $350/night\n"
        "  ★★★★   Comfort Suites        Midtown       $180/night\n"
        "  ★★★    Budget Inn            Near Transit  $95/night\n"
        "\n"
        "Best value: Comfort Suites — central location, breakfast included."
    )


@mcp.tool()
def get_hotel_details(hotel_name: str) -> str:
    """Get full details for a hotel: room count, amenities, check-in/out, transport links."""
    key = hotel_name.lower().strip()
    for k, v in _DETAILS.items():
        if k in key or key in k:
            return v
    return (
        f"{hotel_name}\n"
        "  Details not available in local cache.\n"
        "  Check the hotel's official website for current rates and amenities."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")

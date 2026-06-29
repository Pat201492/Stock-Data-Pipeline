"""
commodity_trade.py — Reference: top exporters / importers per commodity
========================================================================
Exporter/importer shares are NOT market data — they're curated reference
figures (approximate, recent years; sources: OEC / EIA / USGS / USDA / ITC).
Treat as indicative, not exact. Served via GET /api/commodities/{root}.

Shape: root -> {"unit": "...", "exporters": [(country, pct)], "importers": [...]}
pct = approximate share of global trade (%). None where not meaningfully traded
as a standardized global commodity (e.g. US-centric gasoline/heating oil, milk).
"""

TRADE = {
    "CL=F": {"unit": "crude oil",
        "exporters": [("Saudi Arabia", 15), ("Russia", 12), ("United States", 10), ("Canada", 9), ("Iraq", 8), ("UAE", 6)],
        "importers": [("China", 22), ("United States", 13), ("India", 9), ("South Korea", 7), ("Japan", 6), ("Netherlands", 5)]},
    "BZ=F": {"unit": "crude oil (Brent benchmark)",
        "exporters": [("Saudi Arabia", 15), ("Russia", 12), ("United States", 10), ("Canada", 9), ("Iraq", 8), ("UAE", 6)],
        "importers": [("China", 22), ("United States", 13), ("India", 9), ("South Korea", 7), ("Japan", 6)]},
    "NG=F": {"unit": "natural gas (LNG + pipeline)",
        "exporters": [("United States", 21), ("Russia", 15), ("Qatar", 13), ("Australia", 11), ("Norway", 8), ("Canada", 5)],
        "importers": [("China", 15), ("Japan", 13), ("Germany", 8), ("South Korea", 8), ("Italy", 5), ("France", 4)]},
    "GC=F": {"unit": "gold (mine production proxy)",
        "exporters": [("China", 10), ("Australia", 9), ("Russia", 9), ("Canada", 6), ("United States", 5), ("Ghana", 4)],
        "importers": [("China", 22), ("India", 16), ("Switzerland", 12), ("United Kingdom", 10), ("UAE", 6)]},
    "SI=F": {"unit": "silver (mine production proxy)",
        "exporters": [("Mexico", 25), ("China", 14), ("Peru", 13), ("Chile", 6), ("Russia", 5)],
        "importers": [("India", 18), ("United States", 14), ("United Kingdom", 12), ("China", 8), ("Germany", 5)]},
    "PL=F": {"unit": "platinum",
        "exporters": [("South Africa", 72), ("Russia", 11), ("Zimbabwe", 8), ("Canada", 3)],
        "importers": [("China", 30), ("United States", 14), ("Japan", 12), ("Germany", 9)]},
    "PA=F": {"unit": "palladium",
        "exporters": [("Russia", 40), ("South Africa", 35), ("Canada", 8), ("United States", 6)],
        "importers": [("China", 28), ("United States", 16), ("Japan", 12), ("Germany", 10)]},
    "HG=F": {"unit": "copper",
        "exporters": [("Chile", 28), ("Peru", 13), ("DR Congo", 11), ("Australia", 6), ("Russia", 5)],
        "importers": [("China", 50), ("Japan", 7), ("South Korea", 5), ("Germany", 5), ("India", 4)]},
    "ALI=F": {"unit": "aluminum",
        "exporters": [("China", 16), ("Russia", 12), ("Canada", 11), ("UAE", 8), ("India", 7), ("Australia", 6)],
        "importers": [("United States", 14), ("Germany", 9), ("Japan", 8), ("South Korea", 6), ("Netherlands", 5)]},
    "ZC=F": {"unit": "corn",
        "exporters": [("United States", 32), ("Brazil", 22), ("Argentina", 18), ("Ukraine", 12), ("Russia", 2)],
        "importers": [("China", 13), ("Mexico", 12), ("Japan", 10), ("South Korea", 6), ("Vietnam", 5)]},
    "ZW=F": {"unit": "wheat",
        "exporters": [("Russia", 22), ("Australia", 13), ("United States", 11), ("Canada", 11), ("Ukraine", 9), ("EU", 16)],
        "importers": [("Egypt", 6), ("China", 6), ("Indonesia", 6), ("Turkey", 5), ("Algeria", 4)]},
    "ZS=F": {"unit": "soybeans",
        "exporters": [("Brazil", 55), ("United States", 33), ("Argentina", 5), ("Paraguay", 3)],
        "importers": [("China", 60), ("EU", 9), ("Mexico", 4), ("Argentina", 3), ("Egypt", 2)]},
    "KC=F": {"unit": "coffee (green)",
        "exporters": [("Brazil", 30), ("Vietnam", 17), ("Colombia", 8), ("Indonesia", 6), ("Honduras", 5), ("Ethiopia", 5)],
        "importers": [("United States", 17), ("Germany", 13), ("Italy", 7), ("Japan", 6), ("Belgium", 5)]},
    "SB=F": {"unit": "sugar",
        "exporters": [("Brazil", 45), ("Thailand", 11), ("India", 9), ("Australia", 5)],
        "importers": [("Indonesia", 8), ("China", 7), ("United States", 5), ("Bangladesh", 4), ("Algeria", 4)]},
    "CC=F": {"unit": "cocoa beans",
        "exporters": [("Ivory Coast", 38), ("Ghana", 17), ("Ecuador", 11), ("Nigeria", 8), ("Cameroon", 6)],
        "importers": [("Netherlands", 18), ("United States", 11), ("Germany", 11), ("Malaysia", 8), ("Belgium", 6)]},
    "CT=F": {"unit": "cotton",
        "exporters": [("United States", 35), ("Brazil", 24), ("Australia", 10), ("India", 6), ("Greece", 4)],
        "importers": [("China", 24), ("Bangladesh", 16), ("Vietnam", 14), ("Turkey", 9), ("Pakistan", 7)]},
    "ZL=F": {"unit": "soybean oil",
        "exporters": [("Argentina", 42), ("Brazil", 15), ("United States", 9), ("EU", 6)],
        "importers": [("India", 30), ("Bangladesh", 6), ("Algeria", 5), ("Morocco", 4)]},
    "ZM=F": {"unit": "soybean meal",
        "exporters": [("Argentina", 28), ("Brazil", 24), ("United States", 17), ("Paraguay", 4)],
        "importers": [("EU", 18), ("Indonesia", 8), ("Vietnam", 7), ("Thailand", 5)]},
    "LE=F": {"unit": "beef (live cattle proxy)",
        "exporters": [("Brazil", 23), ("Australia", 15), ("United States", 13), ("India", 12), ("Argentina", 7)],
        "importers": [("China", 22), ("United States", 13), ("Japan", 9), ("South Korea", 8)]},
    "HE=F": {"unit": "pork (lean hogs proxy)",
        "exporters": [("EU", 30), ("United States", 27), ("Canada", 11), ("Brazil", 11)],
        "importers": [("China", 24), ("Japan", 12), ("Mexico", 9), ("South Korea", 7)]},
}


def for_root(root):
    return TRADE.get(root)

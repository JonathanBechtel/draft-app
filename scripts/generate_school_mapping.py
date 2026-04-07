#!/usr/bin/env python
"""Generate a draft canonical mapping for college school names.

Queries all distinct school values from players_master, clusters likely
duplicates using normalization heuristics, flags non-college entries,
and outputs a mapping JSON for human review.

Usage:
    python scripts/generate_school_mapping.py
    # Then review scripts/data/school_mapping.json
"""

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

load_dotenv()

OUTPUT_PATH = Path(__file__).parent / "data" / "school_mapping.json"

# ---------------------------------------------------------------------------
# Non-college markers — if a school name contains any of these, map to null
# ---------------------------------------------------------------------------
NON_COLLEGE_MARKERS = [
    "(Turkey)",
    "(Spain)",
    "(CBA)",
    "(NBA G League)",
    "(Serbia)",
    "(EuroLeague)",
    "(professional",
    "(youth academy)",
    "Basket",
    "Bàsquet",
    "Overtime Elite",
    "Breakers",
    "Sturgeons",
    "Loong Lions",
    "Monkey Kings",
    "Capitanes",
    "Swarm",
    "Mega MIS",
    "KK Mega",
    "Panathinaikos",
    "Ratiopharm Ulm",
    "SIG Strasbourg",
    "Valencia Basket",
    "Paris Basketball",
]

# ---------------------------------------------------------------------------
# Known disambiguations — manually curated for tricky cases
# These take precedence over heuristic normalization.
# ---------------------------------------------------------------------------
KNOWN_DISAMBIGUATIONS: dict[str, str | None] = {
    # Miami system — FL vs OH are genuinely different schools
    "Miami (FL)": "Miami (FL)",
    "Miami (Florida)": "Miami (FL)",
    "Miami Hurricanes": "Miami (FL)",
    "University of Miami": "Miami (FL)",
    "Miami (OH)": "Miami (OH)",
    "Miami University": "Miami (OH)",
    # Multi-campus systems — distinct schools, not duplicates
    "University of Wisconsin-Eau Claire": "Wisconsin-Eau Claire",
    "University of Wisconsin-Milwaukee": "UW-Milwaukee",
    "UW-Milwaukee": "UW-Milwaukee",
    "University of Wisconsin-Parkside": "Wisconsin-Parkside",
    "University of Wisconsin-Stevens Point": "Wisconsin-Stevens Point",
    "Wisconsin-River Falls": "Wisconsin-River Falls",
    "University of Minnesota Duluth": "Minnesota Duluth",
    "University of Tennessee at Martin": "UT Martin",
    "University of Arkansas at Pine Bluff": "Arkansas-Pine Bluff",
    "University of Maryland Eastern Shore": "Maryland Eastern Shore",
    "University of Nebraska at Kearney": "Nebraska-Kearney",
    "University of Texas Rio Grande Valley": "UT Rio Grande Valley",
    "University of North Texas": "North Texas",
    "University of North Dakota": "North Dakota",
    # Schools with common short names that ARE the same school
    "University of North Carolina": "UNC",
    "North Carolina": "UNC",
    "University of California, Irvine": "UC Irvine",
    "California State University, Los Angeles": "Cal State LA",
    "University of Illinois Urbana-Champaign": "Illinois",
    "University of Illinois": "Illinois",
    "Illinois Fighting Illini": "Illinois",
    # Other known mappings for clarity
    "University of San Francisco": "San Francisco",
    "University of San Diego": "San Diego",
    "San Diego State": "San Diego State",
    "University of South Alabama": "South Alabama",
    "University of Southern California": "USC",
    "Southern Methodist (SMU)": "SMU",
    "University of Connecticut": "UConn",
    "University of Nevada, Las Vegas": "UNLV",
    "University of Pennsylvania": "Penn",
    "University of Pittsburgh": "Pitt",
    "University of the Pacific": "Pacific",
    "University of the District of Columbia": "District of Columbia",
    "Brigham Young University": "BYU",
    "Brigham Young University Hawaii": "BYU-Hawaii",
    "Louisiana State University": "LSU",
    "Texas Tech Red Raiders": "Texas Tech",
    "Utah State Aggies": "Utah State",
    "Florida Gators": "Florida",
    "Arizona Wildcats": "Arizona",
    "Arkansas Razorbacks": "Arkansas",
    "Oakland Golden Grizzlies": "Oakland",
    "California Western Uiversity": "Cal Western",  # typo in source data
    "California University of Pennslyvania": "Cal U of PA",  # typo in source data
    # Schools where "University" stripping gives wrong results
    "Boston University": "Boston University",
    "Ohio University": "Ohio",
    "Assumption College": "Assumption",
    "Assumption University": "Assumption",
    # Colorado system
    "University of Colorado Boulder": "Colorado",
    "Colorado": "Colorado",
    "Colorado State": "Colorado State",
    # Drake is correct as-is (Drake University → Drake)
    "Drake University": "Drake",
    # International professional — exclude
    "FC Barcelona Bàsquet": None,
    "Real Madrid (Spain)": None,
    "Real Madrid (professional club)": None,
    "Fujian Sturgeons": None,
    "Guangzhou Loong Lions (CBA)": None,
    "Nanjing Monkey Kings": None,
    "New Zealand Breakers": None,
    "Aquila Basket Trento": None,
    "Beşiktaş (Turkey)": None,
    "KK Mega Basket": None,
    "Mega Basket": None,
    "Mega MIS (Serbia)": None,
    "Mega MIS / Crvena zvezda": None,
    "Panathinaikos (professional)": None,
    "Paris Basketball (EuroLeague)": None,
    "Ratiopharm Ulm": None,
    "SIG Strasbourg (youth academy)": None,
    "Valencia Basket": None,
    "Greensboro Swarm (NBA G League)": None,
    "Mexico City Capitanes (NBA G League)": None,
    "Overtime Elite": None,
}


def normalize_school_name(raw: str) -> str:
    """Apply heuristic normalization to a school name.

    Strips common prefixes/suffixes to produce a shorter canonical form.
    Does NOT handle disambiguation — that's done by KNOWN_DISAMBIGUATIONS.
    """
    name = raw.strip()

    # "University of X" → "X"
    if name.startswith("University of "):
        name = name[len("University of ") :]

    # "X University" → "X" (but not "X State University" — handle separately)
    if name.endswith(" University") and not name.endswith(" State University"):
        name = name[: -len(" University")]

    # "X State University" → "X State"
    if name.endswith(" State University"):
        name = name[: -len(" University")]

    # Strip trailing " College" for standalone colleges
    # But keep it if it's part of the proper name (e.g., "Boston College")
    # Heuristic: only strip if the remaining part is a place name (has >1 word)
    # Actually, safer to leave " College" alone — many are proper names

    # Normalize parenthetical state qualifiers
    name = re.sub(r"\(Florida\)", "(FL)", name)
    name = re.sub(r"\(Ohio\)", "(OH)", name)

    return name.strip()


def build_canonical_clusters(
    schools: list[tuple[str, int]],
) -> dict[str, str | None]:
    """Build a mapping from raw school names to canonical names.

    Args:
        schools: List of (school_name, player_count) tuples.

    Returns:
        Dict mapping every raw name to its canonical form (or None for non-college).
    """
    mapping: dict[str, str | None] = {}

    # Step 1: Apply known disambiguations first
    for raw, canonical in KNOWN_DISAMBIGUATIONS.items():
        mapping[raw] = canonical

    # Step 2: Detect non-college by markers
    for raw, _count in schools:
        if raw in mapping:
            continue
        if any(marker in raw for marker in NON_COLLEGE_MARKERS):
            mapping[raw] = None

    # Step 3: Normalize remaining schools and cluster
    # Group by normalized form, pick the most common raw value as canonical
    clusters: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for raw, count in schools:
        if raw in mapping:
            continue
        normalized = normalize_school_name(raw)
        clusters[normalized].append((raw, count))

    for normalized, members in clusters.items():
        # Sort by player count descending — most common form becomes canonical
        members.sort(key=lambda x: x[1], reverse=True)
        canonical = normalized  # Use the normalized form as canonical

        for raw, _count in members:
            mapping[raw] = canonical

    return mapping


async def generate_mapping() -> None:
    """Query DB and generate school mapping JSON."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(db_url)

    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT school, COUNT(*) as cnt "
                "FROM players_master "
                "WHERE school IS NOT NULL "
                "GROUP BY school ORDER BY cnt DESC"
            )
        )
        schools = [(row[0], row[1]) for row in result.fetchall()]

    await engine.dispose()

    print(f"Found {len(schools)} distinct school values")

    # Build the mapping
    mapping = build_canonical_clusters(schools)

    # Compute stats
    canonical_set = {v for v in mapping.values() if v is not None}
    non_college = [k for k, v in mapping.items() if v is None]
    remapped = {k: v for k, v in mapping.items() if v is not None and k != v}

    print(f"Canonical schools: {len(canonical_set)}")
    print(f"Non-college entries: {len(non_college)}")
    print(f"Values that will be remapped: {len(remapped)}")

    # Show remapping preview
    print("\n=== REMAPPING PREVIEW (showing changed values) ===")
    for raw, canonical in sorted(remapped.items()):
        # Find player count for this raw value
        count = next((c for s, c in schools if s == raw), 0)
        print(f"  {raw} ({count}) → {canonical}")

    print(f"\n=== NON-COLLEGE ENTRIES ({len(non_college)}) ===")
    for name in sorted(non_college):
        count = next((c for s, c in schools if s == name), 0)
        print(f"  {name} ({count})")

    # Write mapping file (sorted for easy review)
    sorted_mapping = dict(sorted(mapping.items()))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted_mapping, f, indent=2, ensure_ascii=False)

    print(f"\nMapping written to {OUTPUT_PATH}")
    print(">>> Review this file before proceeding to seed data generation <<<")


if __name__ == "__main__":
    asyncio.run(generate_mapping())

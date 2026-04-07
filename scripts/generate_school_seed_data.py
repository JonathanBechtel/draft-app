#!/usr/bin/env python
"""Generate college school seed data with ESPN IDs, conferences, and colors.

Reads the reviewed school_mapping.json, extracts canonical school names,
and resolves ESPN team metadata via their public API.

Usage:
    python scripts/generate_school_seed_data.py
    # Then review scripts/data/college_schools.json
"""

import json
import re
import time
from pathlib import Path

import httpx

MAPPING_PATH = Path(__file__).parent / "data" / "school_mapping.json"
OUTPUT_PATH = Path(__file__).parent / "data" / "college_schools.json"

# ESPN API endpoints
ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams"
ESPN_SEARCH_URL = "https://site.api.espn.com/apis/common/v3/search"


def slugify(name: str) -> str:
    """Convert a school name to a URL-safe slug."""
    slug = name.lower()
    slug = re.sub(r"[()&]", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def normalize_for_match(name: str) -> str:
    """Normalize a name for fuzzy comparison."""
    s = name.lower()
    # Remove common suffixes/noise
    for suffix in [
        " blue devils",
        " wildcats",
        " bulldogs",
        " tigers",
        " bears",
        " eagles",
        " hawks",
        " huskies",
        " knights",
        " lions",
        " mustangs",
        " panthers",
        " rams",
        " rebels",
        " warriors",
        " wolverines",
    ]:
        s = s.replace(suffix, "")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return s.strip()


def load_espn_teams_bulk() -> list[dict]:
    """Fetch all NCAA teams from ESPN's bulk teams endpoint.

    Returns a list of team dicts with id, displayName, location, abbreviation,
    color, alternateColor.
    """
    teams = []
    # ESPN returns up to 500 teams per page
    for page in range(1, 5):  # pages 1-4 should cover all ~362 teams
        resp = httpx.get(
            ESPN_TEAMS_URL,
            params={"limit": 500, "page": page},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        page_teams = (
            data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        )
        if not page_teams:
            break
        for entry in page_teams:
            t = entry.get("team", {})
            teams.append(t)
    return teams


# Well-known abbreviations where the canonical name won't appear in ESPN's
# displayName/location. Map canonical → ESPN search query.
SEARCH_OVERRIDES: dict[str, str] = {
    "UNC": "North Carolina",
    "Miami (FL)": "Miami Hurricanes",
    "UNC Asheville": "UNC Asheville",
    "UNC Charlotte": "Charlotte 49ers",
    "UNC Wilmington": "UNC Wilmington",
    "USC Upstate": "USC Upstate",
    "UW-Milwaukee": "Milwaukee Panthers",
    "UT Arlington": "UT Arlington",
    "UT Martin": "UT Martin",
    "UT Rio Grande Valley": "UT Rio Grande Valley",
    "SE Missouri State": "Southeast Missouri State",
    "NC Central": "North Carolina Central",
    "BYU": "BYU",
    "UCF": "UCF",
    "UMass": "UMass",
    "UMass Lowell": "UMass Lowell",
    "UNLV": "UNLV",
    "SMU": "SMU",
    "USC": "USC",
    "VCU": "VCU",
    "UAB": "UAB",
    "LSU": "LSU",
    "TCU": "TCU",
    "Loyola Chicago": "Loyola Chicago",
    "Loyola (MD)": "Loyola Maryland",
    "Loyola Marymount": "Loyola Marymount",
    "Long Island": "Long Island University",
    "Appalachian State": "Appalachian State",
    "Austin Peay State": "Austin Peay",
    "Central Florida": "UCF",
    "Texas-El Paso": "UTEP",
    "Texas-San Antonio": "UTSA",
    "Louisiana-Monroe": "Louisiana-Monroe",
    "Missouri-Kansas City": "UMKC",
    "Purdue-Fort Wayne": "Purdue Fort Wayne",
    "Tennessee Technological": "Tennessee Tech",
    "College of Charleston": "Charleston",
    "Dartmouth College": "Dartmouth",
    "Davidson College": "Davidson",
    "Iona College": "Iona",
    "Marist College": "Marist",
    "Providence College": "Providence",
    "Siena College": "Siena",
    "San Jose State": "San Jose State",
    "Bethune-Cookman College": "Bethune-Cookman",
    "Wofford College": "Wofford",
    "United States Military Academy": "Army",
    "United States Naval Academy": "Navy",
    "Virginia Commonwealth": "VCU",
    "Virginia Military Institute": "VMI",
    "Winston-Salem State": "Winston-Salem State",
    "Savannah State": "Savannah State",
    "Houston Baptist": "Houston Christian",
    "Southern University and A&M College": "Southern University",
}


def search_espn_team(name: str) -> dict | None:
    """Search ESPN for a specific team by name.

    Returns the first match or None.
    """
    # Use override query if available
    query = SEARCH_OVERRIDES.get(name, name)

    resp = httpx.get(
        ESPN_SEARCH_URL,
        params={
            "query": query,
            "limit": 3,
            "type": "team",
            "sport": "basketball",
            "league": "mens-college-basketball",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        return None

    items = resp.json().get("items", [])
    if not items:
        return None

    # If we used a SEARCH_OVERRIDE, trust the result
    if name in SEARCH_OVERRIDES:
        return items[0]

    # Otherwise check that the match is reasonable
    item = items[0]
    match_location = normalize_for_match(item.get("location", ""))
    match_display = normalize_for_match(item.get("displayName", ""))
    query_norm = normalize_for_match(name)

    # Accept if the query appears in the location or displayName
    if (
        query_norm in match_location
        or query_norm in match_display
        or match_location in query_norm
    ):
        return item

    return None


def match_school_to_espn(canonical: str, espn_teams: list[dict]) -> dict | None:
    """Try to match a canonical school name to an ESPN team from the bulk list.

    Uses progressively looser matching:
    1. Exact location match (e.g., "Duke" == team.location "Duke")
    2. Normalized match stripping mascots
    """
    canonical_norm = normalize_for_match(canonical)

    for team in espn_teams:
        location = team.get("location", "")
        display = team.get("displayName", "")

        # Exact location match
        if location.lower() == canonical.lower():
            return team

        # Normalized match
        if normalize_for_match(location) == canonical_norm:
            return team
        if normalize_for_match(display) == canonical_norm:
            return team

    return None


def extract_team_data(team: dict) -> dict:
    """Extract standardized fields from an ESPN team dict."""
    team_id = team.get("id") or team.get("collegeId")
    color = team.get("color", "")
    alt_color = team.get("alternateColor", "")

    return {
        "espn_id": int(team_id) if team_id else None,
        "primary_color": f"#{color}" if color and color != "000000" else None,
        "secondary_color": f"#{alt_color}"
        if alt_color and alt_color != "000000"
        else None,
    }


def generate_seed_data() -> None:
    """Generate college school seed data from mapping + ESPN API."""
    # Load mapping
    if not MAPPING_PATH.exists():
        print(f"ERROR: {MAPPING_PATH} not found. Run generate_school_mapping.py first.")
        return

    with open(MAPPING_PATH, encoding="utf-8") as f:
        mapping = json.load(f)

    # Extract unique canonical names (non-null)
    canonicals = sorted({v for v in mapping.values() if v is not None})
    print(f"Canonical schools to process: {len(canonicals)}")

    # Fetch ESPN team catalog
    print("Fetching ESPN team catalog...")
    espn_teams = load_espn_teams_bulk()
    print(f"ESPN teams loaded: {len(espn_teams)}")

    # Match each canonical school
    seed_data: list[dict] = []
    matched = 0
    search_matched = 0
    unmatched = 0

    for i, canonical in enumerate(canonicals):
        slug = slugify(canonical)

        # Try bulk match first
        team = match_school_to_espn(canonical, espn_teams)
        source = "bulk"

        if team is None:
            # Fall back to search API (rate-limited)
            time.sleep(0.5)  # Be nice to ESPN
            result = search_espn_team(canonical)
            if result:
                team = result
                source = "search"

        if team:
            espn_data = extract_team_data(team)
            if source == "bulk":
                matched += 1
            else:
                search_matched += 1

            # Try to get conference from team details for bulk matches
            conference = None
            # For search results, conference isn't in the response
            # We could do a per-team lookup but that's 250+ requests
            # Leave conference null for now — can be enriched later

            entry = {
                "name": canonical,
                "slug": slug,
                "conference": conference,
                "espn_id": espn_data["espn_id"],
                "primary_color": espn_data["primary_color"],
                "secondary_color": espn_data["secondary_color"],
            }
        else:
            unmatched += 1
            entry = {
                "name": canonical,
                "slug": slug,
                "conference": None,
                "espn_id": None,
                "primary_color": None,
                "secondary_color": None,
            }

        seed_data.append(entry)

        # Progress every 50
        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(canonicals)}...")

    # Now enrich conference data via per-team API for matched schools
    print("\nEnriching conference data for matched schools...")
    enriched = 0
    matched_entries = [e for e in seed_data if e["espn_id"] is not None]
    for i, entry in enumerate(matched_entries):
        try:
            time.sleep(0.3)  # Rate limit
            resp = httpx.get(
                f"{ESPN_TEAMS_URL}/{entry['espn_id']}",
                timeout=15.0,
            )
            if resp.status_code == 200:
                team_detail = resp.json().get("team", {})
                # Conference is in standingSummary: "1st in ACC"
                summary = team_detail.get("standingSummary", "")
                if " in " in summary:
                    conf_name = summary.split(" in ", 1)[1]
                    entry["conference"] = conf_name
                    enriched += 1
        except Exception:
            pass  # Skip enrichment failures silently

        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(matched_entries)} team details...")

    # Write output
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(seed_data, f, indent=2, ensure_ascii=False)

    print("\nResults:")
    print(f"  Bulk matched: {matched}")
    print(f"  Search matched: {search_matched}")
    print(f"  Unmatched (no ESPN): {unmatched}")
    print(f"  Conferences enriched: {enriched}")
    print(f"\nSeed data written to {OUTPUT_PATH}")

    # List unmatched schools
    if unmatched:
        print(f"\n=== UNMATCHED SCHOOLS ({unmatched}) ===")
        for entry in seed_data:
            if entry["espn_id"] is None:
                print(f"  {entry['name']}")


if __name__ == "__main__":
    generate_seed_data()

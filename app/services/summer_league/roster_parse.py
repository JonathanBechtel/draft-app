"""Pure parsing functions for NBA.com Summer League roster pages.

These functions operate on raw HTML strings and return typed data structures.
No network calls are made here; use ``scripts/fetch_summer_league_rosters.py``
for the full fetch→parse→snapshot pipeline.

Source structure (resolved via A0 spike):
- Landing page: ``https://www.nba.com/2026-summer-league-{venus}-roster``
- Team page: ``https://www.nba.com/summer-league/2026/{venue}/team/{TeamID}/{slug}``
- Roster data: ``__NEXT_DATA__.props.pageProps.roster`` (Next.js page blob)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Matches the <script id="__NEXT_DATA__"> JSON blob in NBA.com Next.js pages.
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>\s*(\{.*?\})\s*</script>',
    re.DOTALL,
)

# Matches per-team roster page hrefs embedded in venue landing pages.
# Format: /summer-league/{year}/{venue}/team/{TeamID}/{slug}
_TEAM_LINK_RE = re.compile(r'/summer-league/\d+/[^/\s"\']+/team/(\d+)/([a-z0-9-]+)')


@dataclass
class RosterEntry:
    """One player's announced-roster record from NBA.com ``__NEXT_DATA__``.

    All string fields are normalised from the raw JSON; optional fields are
    ``None`` when the source omits them or sends an empty / whitespace value.
    """

    nba_stats_person_id: str  # PLAYER_ID (stable NBA Stats anchor)
    raw_player_name: str  # PLAYER (display name; used for resolution)
    team_id: str  # TeamID
    jersey: str | None  # NUM
    position: str | None  # POSITION
    height: str | None  # HEIGHT (e.g. "6-2")
    weight: str | None  # WEIGHT (lbs as string)
    birth_date: str | None  # BIRTH_DATE (ISO-like string from API)
    school: str | None  # SCHOOL (college; None for internals/G-League)
    how_acquired: str | None  # HOW_ACQUIRED (e.g. "Draft", "Free Agent")
    league_id: str  # LeagueID — 13 / 15 / 16


def _extract_next_data(html: str) -> dict[str, Any]:
    """Extract and parse the ``__NEXT_DATA__`` JSON blob from a Next.js page.

    Args:
        html: Raw HTML page content.

    Returns:
        Parsed JSON dictionary from the ``__NEXT_DATA__`` script tag.

    Raises:
        ValueError: If the tag is absent or the content is not valid JSON.
    """
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise ValueError("No __NEXT_DATA__ script tag found in HTML")
    try:
        return json.loads(match.group(1))  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in __NEXT_DATA__: {exc}") from exc


def parse_team_links(landing_html: str) -> list[tuple[str, str]]:
    """Extract ``(team_id, slug)`` pairs from a venue landing page.

    Scans all hyperlink hrefs for the pattern
    ``/summer-league/{year}/{venue}/team/{TeamID}/{slug}`` and returns
    deduplicated ``(team_id, slug)`` tuples in first-seen order.

    Args:
        landing_html: Raw HTML of the NBA.com SL venue landing page, e.g.
            ``https://www.nba.com/2026-summer-league-vegas-roster``.

    Returns:
        Deduplicated list of ``(team_id, slug)`` tuples.
    """
    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, str]] = []
    for team_id, slug in _TEAM_LINK_RE.findall(landing_html):
        pair = (team_id, slug)
        if pair not in seen:
            seen.add(pair)
            results.append(pair)
    return results


def parse_roster(team_html: str) -> list[RosterEntry]:
    """Parse a roster from a team's NBA.com Summer League page.

    Extracts ``props.pageProps.roster`` from the embedded ``__NEXT_DATA__``
    JSON blob and converts each row to a typed ``RosterEntry``.

    Returns an empty list when the roster array is absent or empty — the
    normal state before a team's roster has been officially announced.

    Args:
        team_html: Raw HTML of a per-team roster page, e.g.
            ``https://www.nba.com/summer-league/2026/las-vegas/team/{id}/{slug}``.

    Returns:
        List of ``RosterEntry`` instances; empty when the page has no roster.
    """
    data = _extract_next_data(team_html)
    page_props: Any = data.get("props", {}).get("pageProps", {})
    roster_raw: list[Any] = page_props.get("roster") or []

    entries: list[RosterEntry] = []
    for row in roster_raw:
        if not isinstance(row, dict):
            continue
        entries.append(
            RosterEntry(
                nba_stats_person_id=str(row.get("PLAYER_ID") or ""),
                raw_player_name=str(row.get("PLAYER") or ""),
                team_id=str(row.get("TeamID") or ""),
                jersey=_opt_str(row.get("NUM")),
                position=_opt_str(row.get("POSITION")),
                height=_opt_str(row.get("HEIGHT")),
                weight=_opt_str(row.get("WEIGHT")),
                birth_date=_opt_str(row.get("BIRTH_DATE")),
                school=_opt_str(row.get("SCHOOL")),
                how_acquired=_opt_str(row.get("HOW_ACQUIRED")),
                league_id=str(row.get("LeagueID") or ""),
            )
        )
    return entries


def _opt_str(value: Any) -> str | None:
    """Return a stripped non-empty string or ``None``.

    Args:
        value: Any value from a parsed JSON object.

    Returns:
        Stripped string, or ``None`` when the value is absent/empty.
    """
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None

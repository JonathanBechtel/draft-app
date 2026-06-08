"""NBA Stats endpoint helpers for Summer League ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SummerLeagueVenue:
    """Metadata for an NBA.com Summer League identifier."""

    league_id: str
    slug: str
    display_name: str


SUPPORTED_SUMMER_LEAGUES: dict[str, SummerLeagueVenue] = {
    "15": SummerLeagueVenue(
        league_id="15",
        slug="las_vegas",
        display_name="Las Vegas Summer League",
    ),
    "13": SummerLeagueVenue(
        league_id="13",
        slug="california_classic",
        display_name="California Classic",
    ),
    "16": SummerLeagueVenue(
        league_id="16",
        slug="salt_lake_city",
        display_name="Salt Lake City Summer League",
    ),
    "14": SummerLeagueVenue(
        league_id="14",
        slug="orlando",
        display_name="Orlando Pro Summer League",
    ),
}


def normalize_league_id(value: str) -> str:
    """Return a supported Summer League ID or raise ``ValueError``.

    Args:
        value: Raw LeagueID value, such as ``"15"``.

    Returns:
        Canonical NBA.com LeagueID.

    Raises:
        ValueError: If ``value`` does not identify a supported Summer League.
    """
    league_id = value.strip()
    if league_id not in SUPPORTED_SUMMER_LEAGUES:
        supported = ", ".join(sorted(SUPPORTED_SUMMER_LEAGUES))
        raise ValueError(
            f"Unsupported Summer League LeagueID {value!r}; use {supported}"
        )
    return league_id


def normalize_season(value: int | str) -> str:
    """Return the NBA Stats Summer League season string.

    Args:
        value: Four-digit season year.

    Returns:
        Four-digit season string accepted by NBA Stats.

    Raises:
        ValueError: If the season is not four digits.
    """
    season = str(value).strip()
    if not season.isdigit() or len(season) != 4:
        raise ValueError(
            f"Summer League Season must be a four-digit year, got {value!r}"
        )
    return season


def build_leaguegamelog_params(
    *,
    league_id: str,
    season: int | str,
    player_or_team: Literal["P", "T"],
) -> dict[str, str]:
    """Build params for the NBA Stats ``leaguegamelog`` endpoint.

    Args:
        league_id: Summer League NBA Stats LeagueID.
        season: Four-digit Summer League season year.
        player_or_team: ``"P"`` for player rows or ``"T"`` for team rows.

    Returns:
        Complete query parameter dictionary.
    """
    if player_or_team not in {"P", "T"}:
        raise ValueError("player_or_team must be 'P' or 'T'")
    return {
        "Counter": "1000",
        "Direction": "DESC",
        "LeagueID": normalize_league_id(league_id),
        "PlayerOrTeam": player_or_team,
        "Season": normalize_season(season),
        "SeasonType": "Regular Season",
        "Sorter": "DATE",
    }


def build_boxscore_params(game_id: str) -> dict[str, str]:
    """Build shared params for NBA Stats box score endpoints.

    Args:
        game_id: NBA Stats ``GAME_ID``.

    Returns:
        Complete query parameter dictionary for boxscore v2 endpoints.
    """
    return {
        "GameID": game_id.strip(),
        "StartPeriod": "0",
        "EndPeriod": "10",
        "StartRange": "0",
        "EndRange": "28800",
        "RangeType": "0",
    }


def build_playbyplay_params(game_id: str) -> dict[str, str]:
    """Build params for the NBA Stats ``playbyplayv2`` endpoint."""
    return {"GameID": game_id.strip(), "StartPeriod": "0", "EndPeriod": "10"}


def build_shotchart_params(
    *,
    league_id: str,
    season: int | str,
    game_id: str,
) -> dict[str, str]:
    """Build params for the NBA Stats ``shotchartdetail`` endpoint.

    Args:
        league_id: Summer League NBA Stats LeagueID.
        season: Four-digit Summer League season year.
        game_id: NBA Stats ``GAME_ID``.

    Returns:
        Complete query parameter dictionary for FGA shot chart rows.
    """
    return {
        "LeagueID": normalize_league_id(league_id),
        "Season": normalize_season(season),
        "SeasonType": "Regular Season",
        "TeamID": "0",
        "PlayerID": "0",
        "GameID": game_id.strip(),
        "Outcome": "",
        "Location": "",
        "Month": "0",
        "SeasonSegment": "",
        "DateFrom": "",
        "DateTo": "",
        "OpponentTeamID": "0",
        "VsConference": "",
        "VsDivision": "",
        "Position": "",
        "RookieYear": "",
        "GameSegment": "",
        "Period": "0",
        "LastNGames": "0",
        "ContextMeasure": "FGA",
        "PlayerPosition": "",
    }

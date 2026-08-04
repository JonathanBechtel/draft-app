"""Parse the NBA.com schedule feed and apply tournament rounds to SL games.

The ``scheduleleaguev2`` payload carries each game's round via ``gameSubLabel``
(e.g. "Semifinals" / "Championship" / "Consolation"); the game log we ingest
does not. :func:`parse_schedule_rounds` extracts ``game_id -> round`` and
:func:`apply_game_rounds` writes it onto matching ``summer_league_games`` rows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueGame


def parse_schedule_rounds(payload: dict[str, Any]) -> dict[str, str]:
    """Return ``{nba_stats_game_id: round_label}`` for labelled games only.

    Pool-play games (empty ``gameSubLabel``) are omitted, so the result contains
    only true bracket games (Semifinals / Championship / Consolation).

    Args:
        payload: Parsed ``scheduleleaguev2`` JSON response.

    Returns:
        Mapping of NBA Stats ``gameId`` to its tournament round label.
    """
    rounds: dict[str, str] = {}
    schedule = payload.get("leagueSchedule") or {}
    for game_date in schedule.get("gameDates") or []:
        for game in game_date.get("games") or []:
            game_id = str(game.get("gameId") or "").strip()
            sub_label = (game.get("gameSubLabel") or "").strip()
            if game_id and sub_label:
                rounds[game_id] = sub_label
    return rounds


async def apply_game_rounds(db: AsyncSession, rounds: dict[str, str]) -> int:
    """Set ``round_label`` on games matching the parsed schedule rounds.

    Args:
        db: Async database session (caller controls the transaction).
        rounds: ``{nba_stats_game_id: round_label}`` from
            :func:`parse_schedule_rounds`.

    Returns:
        The number of game rows updated.
    """
    updated = 0
    for game_id, label in rounds.items():
        result = await db.execute(
            update(SummerLeagueGame)
            .where(SummerLeagueGame.nba_stats_game_id == game_id)  # type: ignore[arg-type]
            .values(round_label=label)
        )
        updated += result.rowcount or 0  # type: ignore[attr-defined]
    return updated

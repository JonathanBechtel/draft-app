"""Read-side service for the Summer League landing and season-hub pages.

Aggregates competitions, games, and player logs into a season overview (venues,
date range, counts) and simple season leaderboards. The schedule/results and
recent-games lists reuse ``search_games`` from
``app.services.summer_league_games_service`` rather than re-querying here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_games_service import (
    _VENUE_ORDER,
    _enum_str,
    _venue_label,
)

DEFAULT_LEADER_LIMIT = 5
DEFAULT_MIN_GAMES = 2
# Career totals reward longevity, so require a slightly larger sample.
DEFAULT_ALLTIME_MIN_GAMES = 5


@dataclass
class VenueSummary:
    """One venue/competition within a Summer League season."""

    venue_slug: str
    venue: str
    display_name: str
    starts_on: Optional[str]
    ends_on: Optional[str]
    team_count: int
    game_count: int
    data_quality: str


@dataclass
class SeasonOverview:
    """A Summer League season across all its venues."""

    year: int
    venues: list[VenueSummary]
    date_start: Optional[str]
    date_end: Optional[str]
    total_games: int


@dataclass
class LeaderRow:
    """One ranked player on a season leaderboard."""

    player_id: int
    slug: Optional[str]
    name: str
    gp: int
    value: float


@dataclass
class SeasonLeaders:
    """Per-game leaders for a season, by stat category."""

    pts: list[LeaderRow]
    reb: list[LeaderRow]
    ast: list[LeaderRow]


def _iso(d: Optional[date]) -> Optional[str]:
    """Return an ISO date string or ``None``."""
    return d.isoformat() if d else None


async def get_season_years(db: AsyncSession) -> list[int]:
    """Return all Summer League competition years, newest first."""
    rows = await db.execute(
        select(SummerLeagueCompetition.year)  # type: ignore[call-overload]
        .distinct()
        .order_by(SummerLeagueCompetition.year.desc())  # type: ignore[attr-defined]
    )
    return [int(y) for (y,) in rows.all()]


async def get_season_overview(db: AsyncSession, year: int) -> Optional[SeasonOverview]:
    """Return a season's venues with date range and team/game counts.

    Returns ``None`` when no competition exists for ``year``.
    """
    comp = SummerLeagueCompetition
    comp_rows = (
        await db.execute(
            select(
                comp.id,
                comp.venue_slug,
                comp.display_name,
                comp.starts_on,
                comp.ends_on,
                comp.data_quality,
            ).where(comp.year == year)  # type: ignore[call-overload, misc, arg-type]
        )
    ).all()
    if not comp_rows:
        return None

    comp_ids = [r.id for r in comp_rows]

    game_count_rows = (
        await db.execute(
            select(
                SummerLeagueGame.competition_id,
                func.count(),
            )  # type: ignore[call-overload]
            .where(SummerLeagueGame.competition_id.in_(comp_ids))  # type: ignore[attr-defined]
            .group_by(SummerLeagueGame.competition_id)
        )
    ).all()
    game_counts: dict[int, int] = {cid: int(cnt) for cid, cnt in game_count_rows}

    team_count_rows = (
        await db.execute(
            select(
                SummerLeagueTeamEntry.competition_id,
                func.count(),
            )  # type: ignore[call-overload]
            .where(SummerLeagueTeamEntry.competition_id.in_(comp_ids))  # type: ignore[attr-defined]
            .group_by(SummerLeagueTeamEntry.competition_id)
        )
    ).all()
    team_counts: dict[int, int] = {cid: int(cnt) for cid, cnt in team_count_rows}

    venues = [
        VenueSummary(
            venue_slug=r.venue_slug,
            venue=_venue_label(r.venue_slug),
            display_name=r.display_name,
            starts_on=_iso(r.starts_on),
            ends_on=_iso(r.ends_on),
            team_count=int(team_counts.get(r.id, 0)),
            game_count=int(game_counts.get(r.id, 0)),
            data_quality=_enum_str(r.data_quality),
        )
        for r in comp_rows
    ]
    venues.sort(key=lambda v: _VENUE_ORDER.get(v.venue_slug, 99))

    starts = [r.starts_on for r in comp_rows if r.starts_on]
    ends = [r.ends_on for r in comp_rows if r.ends_on]
    return SeasonOverview(
        year=year,
        venues=venues,
        date_start=_iso(min(starts)) if starts else None,
        date_end=_iso(max(ends)) if ends else None,
        total_games=sum(game_counts.values()),
    )


async def _fetch_leader_aggregates(
    db: AsyncSession, *, year: Optional[int], min_games: int
) -> list[Any]:
    """Aggregate resolved, non-DNP player lines (optionally for one year).

    Returns one row per qualifying player with ``gp`` and summed pts/reb/ast.
    """
    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition

    conds: list[Any] = [
        pgl.player_id.isnot(None),  # type: ignore[union-attr]
        pgl.minutes_seconds > 0,  # type: ignore[operator]
    ]
    if year is not None:
        conds.append(comp.year == year)  # type: ignore[arg-type]

    rows = (
        await db.execute(
            select(
                pgl.player_id,
                PlayerMaster.slug,
                PlayerMaster.display_name,
                func.count().label("gp"),
                func.sum(pgl.pts).label("pts"),
                func.sum(pgl.reb).label("reb"),
                func.sum(pgl.ast).label("ast"),
            )  # type: ignore[call-overload, misc]
            .select_from(pgl)
            .join(comp, comp.id == pgl.competition_id)
            .join(PlayerMaster, PlayerMaster.id == pgl.player_id)
            .where(*conds)
            .group_by(pgl.player_id, PlayerMaster.slug, PlayerMaster.display_name)
            .having(func.count() >= min_games)
        )
    ).all()
    return list(rows)


def _rank_leaders(
    rows: list[Any], stat: str, *, per_game: bool, limit: int
) -> list[LeaderRow]:
    """Rank aggregate rows by a stat (per-game average or career total)."""
    ranked: list[tuple[float, LeaderRow]] = []
    for r in rows:
        games = int(r.gp)
        total = getattr(r, stat) or 0
        value = (total / games if games else 0.0) if per_game else float(total)
        ranked.append(
            (
                value,
                LeaderRow(
                    player_id=r.player_id,
                    slug=r.slug,
                    name=r.display_name or "Player",
                    gp=games,
                    value=round(value, 1),
                ),
            )
        )
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in ranked[:limit]]


async def get_season_leaders(
    db: AsyncSession,
    year: int,
    *,
    limit: int = DEFAULT_LEADER_LIMIT,
    min_games: int = DEFAULT_MIN_GAMES,
) -> SeasonLeaders:
    """Return per-game PTS/REB/AST leaders for one season."""
    rows = await _fetch_leader_aggregates(db, year=year, min_games=min_games)
    return SeasonLeaders(
        pts=_rank_leaders(rows, "pts", per_game=True, limit=limit),
        reb=_rank_leaders(rows, "reb", per_game=True, limit=limit),
        ast=_rank_leaders(rows, "ast", per_game=True, limit=limit),
    )


async def get_alltime_leaders(
    db: AsyncSession,
    *,
    limit: int = DEFAULT_LEADER_LIMIT,
    min_games: int = DEFAULT_ALLTIME_MIN_GAMES,
) -> SeasonLeaders:
    """Return career (all-season) PTS/REB/AST total leaders."""
    rows = await _fetch_leader_aggregates(db, year=None, min_games=min_games)
    return SeasonLeaders(
        pts=_rank_leaders(rows, "pts", per_game=False, limit=limit),
        reb=_rank_leaders(rows, "reb", per_game=False, limit=limit),
        ast=_rank_leaders(rows, "ast", per_game=False, limit=limit),
    )

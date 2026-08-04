"""Read-side service for the Summer League franchise-history page.

Franchise (`/stats/summer-league/teams/{team}`): one NBA franchise's full,
cross-year Summer League résumé. ``{team}`` is the canonical ``nba_teams.slug``
(e.g. ``lakers``), not the per-competition team-entry slug.

A franchise fields at most one entry per competition (the split/select squads
that play two rosters in one event are unmapped and excluded), so per-entry
records sum cleanly into an all-time record with no double counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.team_logos import franchise_logo_url
from app.services.summer_league_games_service import _enum_str, _venue_label

# Career leaders shown on the franchise page; the rest fold into "All players".
FRANCHISE_LEADER_LIMIT = 10


# --------------------------------------------------------------------------- #
# DTOs
# --------------------------------------------------------------------------- #


@dataclass
class FranchiseSeasonRow:
    """One franchise's run at a single venue-year, linking to the team-season."""

    year: int
    venue_slug: str
    venue: str
    team_slug: str
    wins: int
    losses: int
    points_for: int
    points_against: int
    data_quality: str
    top_performer_name: Optional[str] = None
    top_performer_slug: Optional[str] = None
    top_performer_ppg: Optional[float] = None


@dataclass
class FranchisePlayerRow:
    """One player's career line in this franchise's SL uniforms."""

    slug: Optional[str]
    name: str
    seasons: int
    gp: int
    pts: int
    reb: int
    ast: int
    ppg: Optional[float]


@dataclass
class FranchiseHistory:
    """A franchise's full cross-year Summer League history."""

    slug: str
    name: str
    logo_url: Optional[str]
    all_time_wins: int
    all_time_losses: int
    season_count: int
    player_count: int
    seasons: list[FranchiseSeasonRow] = field(default_factory=list)
    leaders: list[FranchisePlayerRow] = field(default_factory=list)
    players: list[FranchisePlayerRow] = field(default_factory=list)


def _ppg(pts: int, gp: int) -> Optional[float]:
    """Points per game, or ``None`` when no games played."""
    if not gp:
        return None
    return round(pts / gp, 1)


def _player_identity_key() -> Any:
    """A grouping key that collapses a resolved player's logs into one row.

    Resolved logs group by ``player_id`` (so the same canonical player isn't
    split when NBA feed names vary across games/years); only unresolved logs
    (no ``player_id``) fall back to ``raw_player_name``. Prefixes keep the two
    namespaces from ever colliding.
    """
    pgl = SummerLeaguePlayerGameLog
    return func.coalesce(
        func.concat("p", cast(pgl.player_id, String)),
        func.concat("r", pgl.raw_player_name),
    )


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


async def get_franchise_history(
    db: AsyncSession, team_slug: str
) -> Optional[FranchiseHistory]:
    """Return one franchise's cross-year SL record, by-season rows, and players.

    Args:
        db: Async database session.
        team_slug: Canonical ``nba_teams.slug`` (e.g. ``lakers``).

    Returns:
        A populated :class:`FranchiseHistory`, or ``None`` when the slug is not a
        known franchise or has never appeared in Summer League.
    """
    nt = NbaTeam
    franchise = (
        await db.execute(
            select(nt.id, nt.name, nt.slug).where(nt.slug == team_slug)  # type: ignore[call-overload]
        )
    ).first()
    if franchise is None:
        return None

    te = SummerLeagueTeamEntry
    comp = SummerLeagueEdition
    entry_rows = (
        await db.execute(
            select(
                te.id,
                te.team_slug,
                te.nba_stats_team_id,
                comp.year,
                comp.venue_slug,
                comp.data_quality,
            )  # type: ignore[call-overload, misc]
            .select_from(te)
            .join(comp, comp.id == te.competition_id)
            .where(te.nba_team_id == franchise.id)  # type: ignore[arg-type]
            .order_by(comp.year.desc(), comp.venue_slug)  # type: ignore[attr-defined]
        )
    ).all()
    if not entry_rows:
        return None

    entry_ids = [r.id for r in entry_rows]
    logo_url = next(
        (
            url
            for r in entry_rows
            if (url := franchise_logo_url(r.nba_stats_team_id)) is not None
        ),
        None,
    )

    # Per-entry win/loss + points-for/against, computed from game scores.
    game = SummerLeagueGame
    game_rows = (
        await db.execute(
            select(
                game.home_team_entry_id,
                game.away_team_entry_id,
                game.home_score,
                game.away_score,
            ).where(  # type: ignore[call-overload]
                or_(
                    game.home_team_entry_id.in_(entry_ids),  # type: ignore[union-attr]
                    game.away_team_entry_id.in_(entry_ids),  # type: ignore[union-attr]
                )
            )
        )
    ).all()

    rec: dict[int, list[int]] = {eid: [0, 0, 0, 0] for eid in entry_ids}  # w,l,pf,pa
    entry_set = set(entry_ids)
    for g in game_rows:
        if g.home_score is None or g.away_score is None:
            continue
        if g.home_team_entry_id in entry_set:
            eid, mine, opp = g.home_team_entry_id, g.home_score, g.away_score
        else:
            eid, mine, opp = g.away_team_entry_id, g.away_score, g.home_score
        slot = rec[eid]
        slot[0 if mine > opp else 1] += 1
        slot[2] += mine
        slot[3] += opp

    # Top performer per entry: the player with the most total points.
    pgl = SummerLeaguePlayerGameLog
    perf_rows = (
        await db.execute(
            select(
                pgl.team_entry_id,
                func.min(PlayerMaster.slug).label("slug"),
                func.min(PlayerMaster.display_name).label("display_name"),
                func.min(pgl.raw_player_name).label("raw_player_name"),
                func.count().label("gp"),
                func.sum(pgl.pts).label("pts"),
            )  # type: ignore[call-overload, misc]
            .select_from(pgl)
            .join(PlayerMaster, PlayerMaster.id == pgl.player_id, isouter=True)
            .where(
                pgl.team_entry_id.in_(entry_ids),  # type: ignore[attr-defined]
                pgl.minutes_seconds > 0,  # type: ignore[operator]
            )
            .group_by(pgl.team_entry_id, _player_identity_key())
        )
    ).all()

    # Keep the highest-scoring player per entry (total points as the ranker).
    top_by_entry: dict[int, tuple[str, Optional[str], Optional[float]]] = {}
    top_pts: dict[int, int] = {}
    for r in perf_rows:
        pts = int(r.pts or 0)
        if r.team_entry_id not in top_pts or pts > top_pts[r.team_entry_id]:
            top_pts[r.team_entry_id] = pts
            top_by_entry[r.team_entry_id] = (
                r.display_name or r.raw_player_name or "—",
                r.slug,
                _ppg(pts, int(r.gp)),
            )

    seasons: list[FranchiseSeasonRow] = []
    for r in entry_rows:
        w, lo, pf, pa = rec[r.id]
        top = top_by_entry.get(r.id)
        seasons.append(
            FranchiseSeasonRow(
                year=r.year,
                venue_slug=r.venue_slug,
                venue=_venue_label(r.venue_slug),
                team_slug=r.team_slug,
                wins=w,
                losses=lo,
                points_for=pf,
                points_against=pa,
                data_quality=_enum_str(r.data_quality),
                top_performer_name=top[0] if top else None,
                top_performer_slug=top[1] if top else None,
                top_performer_ppg=top[2] if top else None,
            )
        )

    all_w = sum(rec[e][0] for e in entry_ids)
    all_l = sum(rec[e][1] for e in entry_ids)

    # Career player aggregates across every entry this franchise fielded.
    player_rows = (
        await db.execute(
            select(
                func.min(PlayerMaster.slug).label("slug"),
                func.min(PlayerMaster.display_name).label("display_name"),
                func.min(pgl.raw_player_name).label("raw_player_name"),
                func.count(func.distinct(pgl.competition_id)).label("seasons"),
                func.count().label("gp"),
                func.sum(pgl.pts).label("pts"),
                func.sum(pgl.reb).label("reb"),
                func.sum(pgl.ast).label("ast"),
            )  # type: ignore[call-overload, misc]
            .select_from(pgl)
            .join(PlayerMaster, PlayerMaster.id == pgl.player_id, isouter=True)  # type: ignore[arg-type]
            .where(
                pgl.team_entry_id.in_(entry_ids),  # type: ignore[attr-defined]
                pgl.minutes_seconds > 0,  # type: ignore[operator, arg-type]
            )
            .group_by(_player_identity_key())
        )
    ).all()

    players: list[FranchisePlayerRow] = []
    for r in player_rows:
        gp = int(r.gp)
        pts = int(r.pts or 0)
        players.append(
            FranchisePlayerRow(
                slug=r.slug,
                name=r.display_name or r.raw_player_name or "—",
                seasons=int(r.seasons),
                gp=gp,
                pts=pts,
                reb=int(r.reb or 0),
                ast=int(r.ast or 0),
                ppg=_ppg(pts, gp),
            )
        )

    leaders = sorted(players, key=lambda p: p.pts, reverse=True)[
        :FRANCHISE_LEADER_LIMIT
    ]
    players.sort(key=lambda p: p.name.lower())

    return FranchiseHistory(
        slug=franchise.slug,
        name=franchise.name,
        logo_url=logo_url,
        all_time_wins=all_w,
        all_time_losses=all_l,
        season_count=len(seasons),
        player_count=len(players),
        seasons=seasons,
        leaders=leaders,
        players=players,
    )

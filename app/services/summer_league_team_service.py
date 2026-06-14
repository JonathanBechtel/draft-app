"""Read-side service for Summer League venue and team-season pages.

* Venue (`/stats/summer-league/{year}/{venue}`): competition meta, computed
  standings, and the teams that played.
* Team-season (`/stats/summer-league/{year}/{venue}/{team}`): one team's record,
  quick stats, roster, and schedule for a single venue-year.

Standings and records are computed from normalized game scores (the stored
``wins``/``losses`` on team entries are not relied upon).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_games_service import _enum_str, _venue_label
from app.services.summer_league_season_service import _iso


def _ratio_pct(num: float, den: float) -> Optional[float]:
    """Percentage (0-100) of num/den, or ``None`` when den is zero."""
    if not den:
        return None
    return round(100.0 * num / den, 1)


# --------------------------------------------------------------------------- #
# DTOs
# --------------------------------------------------------------------------- #


@dataclass
class TeamStanding:
    """One team's record within a venue competition."""

    team_slug: str
    name: str
    wins: int
    losses: int
    points_for: int
    points_against: int


@dataclass
class VenueDetail:
    """A single Summer League venue within a season."""

    year: int
    venue_slug: str
    venue: str
    date_start: Optional[str]
    date_end: Optional[str]
    data_quality: str
    standings: list[TeamStanding]


@dataclass
class RosterRow:
    """One player's per-game line for a team-season."""

    slug: Optional[str]
    name: str
    gp: int
    ppg: Optional[float]
    rpg: Optional[float]
    apg: Optional[float]
    fg_pct: Optional[float]


@dataclass
class TeamGameRow:
    """One game on a team-season schedule."""

    game_id: int
    game_date: Optional[str]
    opponent: Optional[str]
    is_home: bool
    team_score: Optional[int]
    opp_score: Optional[int]
    result: Optional[str]


@dataclass
class TeamSeason:
    """One team's full run at a single venue-year."""

    year: int
    venue_slug: str
    venue: str
    team_slug: str
    name: str
    wins: int
    losses: int
    ppg: Optional[float]
    opp_ppg: Optional[float]
    roster: list[RosterRow] = field(default_factory=list)
    schedule: list[TeamGameRow] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Venue
# --------------------------------------------------------------------------- #


async def get_venue(
    db: AsyncSession, year: int, venue_slug: str
) -> Optional[VenueDetail]:
    """Return one venue's competition meta + computed standings, or ``None``."""
    comp = SummerLeagueCompetition
    header = (
        await db.execute(
            select(
                comp.id,
                comp.starts_on,
                comp.ends_on,
                comp.data_quality,
            ).where(comp.year == year, comp.venue_slug == venue_slug)  # type: ignore[call-overload, misc, arg-type]
        )
    ).first()
    if header is None:
        return None

    team_rows = (
        await db.execute(
            select(
                SummerLeagueTeamEntry.id,
                SummerLeagueTeamEntry.team_slug,
                SummerLeagueTeamEntry.raw_team_name,
            ).where(SummerLeagueTeamEntry.competition_id == header.id)  # type: ignore[call-overload, arg-type]
        )
    ).all()
    teams = {r.id: r for r in team_rows}

    records: dict[int, dict[str, int]] = {
        tid: {"w": 0, "l": 0, "pf": 0, "pa": 0} for tid in teams
    }
    game_rows = (
        await db.execute(
            select(
                SummerLeagueGame.home_team_entry_id,
                SummerLeagueGame.away_team_entry_id,
                SummerLeagueGame.home_score,
                SummerLeagueGame.away_score,
            ).where(SummerLeagueGame.competition_id == header.id)  # type: ignore[call-overload]  # type: ignore[arg-type]
        )
    ).all()
    for g in game_rows:
        if g.home_score is None or g.away_score is None:
            continue
        h, a = g.home_team_entry_id, g.away_team_entry_id
        if h in records:
            records[h]["pf"] += g.home_score
            records[h]["pa"] += g.away_score
            records[h]["w" if g.home_score > g.away_score else "l"] += 1
        if a in records:
            records[a]["pf"] += g.away_score
            records[a]["pa"] += g.home_score
            records[a]["w" if g.away_score > g.home_score else "l"] += 1

    standings = [
        TeamStanding(
            team_slug=teams[tid].team_slug,
            name=teams[tid].raw_team_name or "—",
            wins=rec["w"],
            losses=rec["l"],
            points_for=rec["pf"],
            points_against=rec["pa"],
        )
        for tid, rec in records.items()
    ]
    # Best record first; win pct then wins as tie-breaks.
    standings.sort(
        key=lambda s: (
            s.wins / (s.wins + s.losses) if (s.wins + s.losses) else 0.0,
            s.wins,
        ),
        reverse=True,
    )

    return VenueDetail(
        year=year,
        venue_slug=venue_slug,
        venue=_venue_label(venue_slug),
        date_start=_iso(header.starts_on),
        date_end=_iso(header.ends_on),
        data_quality=_enum_str(header.data_quality),
        standings=standings,
    )


# --------------------------------------------------------------------------- #
# Team-season
# --------------------------------------------------------------------------- #


async def get_team_season(
    db: AsyncSession, year: int, venue_slug: str, team_slug: str
) -> Optional[TeamSeason]:
    """Return one team's venue-year record, quick stats, roster, and schedule."""
    comp = SummerLeagueCompetition
    te = SummerLeagueTeamEntry
    header = (
        await db.execute(
            select(
                te.id,
                te.raw_team_name,
                comp.id,
            )  # type: ignore[call-overload, misc]
            .select_from(te)
            .join(comp, comp.id == te.competition_id)
            .where(
                comp.year == year,  # type: ignore[arg-type]
                comp.venue_slug == venue_slug,
                te.team_slug == team_slug,
            )
        )
    ).first()
    if header is None:
        return None

    team_entry_id = header.id
    opponent = aliased(SummerLeagueTeamEntry)
    game = SummerLeagueGame
    schedule_rows = (
        await db.execute(
            select(
                game.id,
                game.game_date,
                game.home_team_entry_id,
                game.home_score,
                game.away_score,
                opponent.raw_team_abbreviation,
                opponent.raw_team_name,
            )  # type: ignore[call-overload, misc]
            .select_from(game)
            .join(
                opponent,
                opponent.id  # type: ignore[arg-type]
                == case(
                    (  # type: ignore[arg-type]
                        game.home_team_entry_id == team_entry_id,
                        game.away_team_entry_id,
                    ),
                    else_=game.home_team_entry_id,
                ),
                isouter=True,
            )
            .where(
                or_(
                    game.home_team_entry_id == team_entry_id,  # type: ignore[arg-type]
                    game.away_team_entry_id == team_entry_id,  # type: ignore[arg-type]
                )
            )
            .order_by(desc(game.game_date), desc(game.id))  # type: ignore[arg-type]
        )
    ).all()

    schedule: list[TeamGameRow] = []
    wins = losses = pf = pa = played = 0
    for r in schedule_rows:
        is_home = r.home_team_entry_id == team_entry_id
        team_score = r.home_score if is_home else r.away_score
        opp_score = r.away_score if is_home else r.home_score
        result: Optional[str] = None
        if team_score is not None and opp_score is not None:
            played += 1
            pf += team_score
            pa += opp_score
            if team_score > opp_score:
                wins += 1
                result = "W"
            else:
                losses += 1
                result = "L"
        schedule.append(
            TeamGameRow(
                game_id=r.id,
                game_date=r.game_date.isoformat() if r.game_date else None,
                opponent=r.raw_team_abbreviation or r.raw_team_name,
                is_home=is_home,
                team_score=team_score,
                opp_score=opp_score,
                result=result,
            )
        )

    pgl = SummerLeaguePlayerGameLog
    roster_rows = (
        await db.execute(
            select(
                pgl.player_id,
                PlayerMaster.slug,
                PlayerMaster.display_name,
                pgl.raw_player_name,
                func.count().label("gp"),
                func.sum(pgl.pts).label("pts"),
                func.sum(pgl.reb).label("reb"),
                func.sum(pgl.ast).label("ast"),
                func.sum(pgl.fgm).label("fgm"),
                func.sum(pgl.fga).label("fga"),
            )  # type: ignore[call-overload, misc]
            .select_from(pgl)
            .join(PlayerMaster, PlayerMaster.id == pgl.player_id, isouter=True)
            .where(
                pgl.team_entry_id == team_entry_id,  # type: ignore[arg-type]
                pgl.minutes_seconds > 0,  # type: ignore[operator]
            )
            .group_by(
                pgl.player_id,
                PlayerMaster.slug,
                PlayerMaster.display_name,
                pgl.raw_player_name,
            )
        )
    ).all()

    roster: list[RosterRow] = []
    for r in roster_rows:
        gp = int(r.gp)
        roster.append(
            RosterRow(
                slug=r.slug,
                name=r.display_name or r.raw_player_name or "—",
                gp=gp,
                ppg=round((r.pts or 0) / gp, 1) if gp else None,
                rpg=round((r.reb or 0) / gp, 1) if gp else None,
                apg=round((r.ast or 0) / gp, 1) if gp else None,
                fg_pct=_ratio_pct(r.fgm or 0, r.fga or 0),
            )
        )
    roster.sort(key=lambda x: x.ppg or 0.0, reverse=True)

    return TeamSeason(
        year=year,
        venue_slug=venue_slug,
        venue=_venue_label(venue_slug),
        team_slug=team_slug,
        name=header.raw_team_name or "—",
        wins=wins,
        losses=losses,
        ppg=round(pf / played, 1) if played else None,
        opp_ppg=round(pa / played, 1) if played else None,
        roster=roster,
        schedule=schedule,
    )

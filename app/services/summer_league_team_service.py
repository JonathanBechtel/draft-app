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

from app.schemas.nba_teams import NbaTeam
from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.team_logos import franchise_logo_url
from app.services.summer_league_games_service import _enum_str, _venue_label
from app.services.summer_league_season_service import _iso


def _ratio_pct(num: float, den: float) -> Optional[float]:
    """Percentage (0-100) of num/den, or ``None`` when den is zero."""
    if not den:
        return None
    return round(100.0 * num / den, 1)


def _jersey_sort_key(jersey: Optional[str]) -> tuple[int, int, str]:
    """Sort key ordering numeric jerseys first, then non-numeric, then blanks."""
    if jersey is None or jersey == "":
        return (2, 0, "")
    try:
        return (0, int(jersey), "")
    except ValueError:
        return (1, 0, jersey)


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
    logo_url: Optional[str] = None


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
    # Per-game shooting volume (made/attempted) shown as separate columns.
    fgm: Optional[float]
    fga: Optional[float]
    fg3m: Optional[float]
    fg3a: Optional[float]
    ftm: Optional[float]
    fta: Optional[float]
    fg_pct: Optional[float]


@dataclass
class AnnouncedRosterRow:
    """One announced/confirmed roster slot for a team-season (pre-/early-event).

    Sourced from ``summer_league_participation`` assertions rather than box
    scores, so it is populated before any game is played (the A4 preview). As
    games tip the same player surfaces in the stats-derived :class:`RosterRow`.
    """

    slug: Optional[str]
    name: str
    jersey: Optional[str]
    position: Optional[str]
    status: str
    headshot_url: Optional[str]


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
    logo_url: Optional[str] = None
    franchise_slug: Optional[str] = None
    roster: list[RosterRow] = field(default_factory=list)
    announced_roster: list[AnnouncedRosterRow] = field(default_factory=list)
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
                SummerLeagueTeamEntry.nba_stats_team_id,
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
            logo_url=franchise_logo_url(teams[tid].nba_stats_team_id),
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
                te.nba_stats_team_id,
                comp.id,
                NbaTeam.slug.label("franchise_slug"),  # type: ignore[attr-defined]
            )  # type: ignore[call-overload, misc]
            .select_from(te)
            .join(comp, comp.id == te.competition_id)
            .join(NbaTeam, NbaTeam.id == te.nba_team_id, isouter=True)
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
                func.sum(pgl.fg3m).label("fg3m"),
                func.sum(pgl.fg3a).label("fg3a"),
                func.sum(pgl.ftm).label("ftm"),
                func.sum(pgl.fta).label("fta"),
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
                fgm=round((r.fgm or 0) / gp, 1) if gp else None,
                fga=round((r.fga or 0) / gp, 1) if gp else None,
                fg3m=round((r.fg3m or 0) / gp, 1) if gp else None,
                fg3a=round((r.fg3a or 0) / gp, 1) if gp else None,
                ftm=round((r.ftm or 0) / gp, 1) if gp else None,
                fta=round((r.fta or 0) / gp, 1) if gp else None,
                fg_pct=_ratio_pct(r.fgm or 0, r.fga or 0),
            )
        )
    roster.sort(key=lambda x: x.ppg or 0.0, reverse=True)

    # Announced roster (A4 preview): participation assertions, populated before
    # any box score exists. CUT slots are excluded; the rest surface with their
    # current status so the page transitions announced -> confirmed as games tip.
    part = SummerLeagueParticipation
    sp = SummerLeagueSourcePlayer
    announced_rows = (
        await db.execute(
            select(
                part.jersey_number,
                part.roster_position,
                part.roster_status,
                PlayerMaster.slug,
                PlayerMaster.display_name,
                PlayerMaster.reference_image_url,
                sp.raw_player_name,
            )  # type: ignore[call-overload, misc]
            .select_from(part)
            .join(sp, sp.id == part.source_player_id)
            .join(PlayerMaster, PlayerMaster.id == part.player_id, isouter=True)
            .where(
                part.team_entry_id == team_entry_id,  # type: ignore[arg-type]
                part.roster_status != AffiliationStatus.CUT,  # type: ignore[arg-type]
            )
        )
    ).all()

    announced_roster: list[AnnouncedRosterRow] = []
    for r in announced_rows:
        announced_roster.append(
            AnnouncedRosterRow(
                slug=r.slug,
                name=r.display_name or r.raw_player_name or "—",
                jersey=r.jersey_number,
                position=r.roster_position,
                status=_enum_str(r.roster_status) or "",
                headshot_url=r.reference_image_url,
            )
        )
    announced_roster.sort(key=lambda x: (_jersey_sort_key(x.jersey), x.name.lower()))

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
        logo_url=franchise_logo_url(header.nba_stats_team_id),
        franchise_slug=header.franchise_slug,
        roster=roster,
        announced_roster=announced_roster,
        schedule=schedule,
    )


# --------------------------------------------------------------------------- #
# Venue bracket (Las Vegas championship)
# --------------------------------------------------------------------------- #

# Rounds that make up the championship bracket (consolation/placement games are
# excluded — they're not part of the title path).
CHAMPIONSHIP_ROUNDS = ("Semifinals", "Championship")


@dataclass
class BracketGame:
    """One game in a venue's championship bracket."""

    game_id: int
    home_name: str
    home_abbr: Optional[str]
    home_logo: Optional[str]
    home_score: Optional[int]
    away_name: str
    away_abbr: Optional[str]
    away_logo: Optional[str]
    away_score: Optional[int]
    winner: Optional[str]  # "home" | "away" | None


@dataclass
class Bracket:
    """A venue's championship bracket (semifinals + final)."""

    year: int
    semifinals: list[BracketGame] = field(default_factory=list)
    final: Optional[BracketGame] = None


def _winner(home_score: Optional[int], away_score: Optional[int]) -> Optional[str]:
    """Return ``"home"``/``"away"`` for the higher score, or ``None``."""
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return None


async def get_venue_bracket(
    db: AsyncSession, year: int, venue_slug: str
) -> Optional[Bracket]:
    """Return a venue's championship bracket, or ``None`` when it has none.

    Only Las Vegas runs a true bracket; other venues (and years without schedule
    enrichment) have no ``round_label`` games and return ``None``.
    """
    home = aliased(SummerLeagueTeamEntry)
    away = aliased(SummerLeagueTeamEntry)
    game = SummerLeagueGame
    comp = SummerLeagueCompetition

    rows = (
        await db.execute(
            select(
                game.id,
                game.round_label,
                game.home_score,
                game.away_score,
                home.raw_team_name.label("home_name"),  # type: ignore[attr-defined]
                home.raw_team_abbreviation.label("home_abbr"),  # type: ignore[union-attr]
                home.nba_stats_team_id.label("home_sid"),  # type: ignore[attr-defined]
                away.raw_team_name.label("away_name"),  # type: ignore[attr-defined]
                away.raw_team_abbreviation.label("away_abbr"),  # type: ignore[union-attr]
                away.nba_stats_team_id.label("away_sid"),  # type: ignore[attr-defined]
            )  # type: ignore[call-overload, misc]
            .select_from(game)
            .join(comp, comp.id == game.competition_id)
            .join(home, home.id == game.home_team_entry_id, isouter=True)
            .join(away, away.id == game.away_team_entry_id, isouter=True)
            .where(
                comp.year == year,  # type: ignore[arg-type]
                comp.venue_slug == venue_slug,
                game.round_label.in_(CHAMPIONSHIP_ROUNDS),  # type: ignore[union-attr]
            )
            .order_by(game.game_date, game.id)  # type: ignore[arg-type]
        )
    ).all()
    if not rows:
        return None

    bracket = Bracket(year=year)
    for r in rows:
        bg = BracketGame(
            game_id=r.id,
            home_name=r.home_name or "—",
            home_abbr=r.home_abbr,
            home_logo=franchise_logo_url(r.home_sid),
            home_score=r.home_score,
            away_name=r.away_name or "—",
            away_abbr=r.away_abbr,
            away_logo=franchise_logo_url(r.away_sid),
            away_score=r.away_score,
            winner=_winner(r.home_score, r.away_score),
        )
        if r.round_label == "Championship":
            bracket.final = bg
        else:
            bracket.semifinals.append(bg)
    return bracket

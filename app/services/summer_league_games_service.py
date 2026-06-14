"""Read-side service for the Summer League games store.

Powers three surfaces (see ``docs/plans/summer-league-games-index-spec.md``):

* The global, filterable **games index** (`/stats/summer-league/games`).
* A single **game box score** (`/stats/summer-league/{year}/games/{game_id}`).
* A player's complete **game logs** (`/players/{slug}/summer-league`).

Read-only and raw-data-only: it queries the normalized Summer League tables and
reshapes rows for templates. No composite metrics are computed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)

# Human-readable venue labels keyed by the competition ``venue_slug``.
VENUE_LABELS: dict[str, str] = {
    "las_vegas": "Las Vegas",
    "salt_lake_city": "Salt Lake City",
    "california_classic": "California Classic",
    "orlando": "Orlando",
}

# Display order within a year (marquee Las Vegas first).
_VENUE_ORDER: dict[str, int] = {
    "las_vegas": 0,
    "salt_lake_city": 1,
    "california_classic": 2,
    "orlando": 3,
}

DEFAULT_PAGE_SIZE = 30


def _venue_label(slug: Optional[str]) -> str:
    """Return a human venue label for a slug, falling back to the slug itself."""
    if not slug:
        return "—"
    return VENUE_LABELS.get(slug, slug.replace("_", " ").title())


def _minutes(minutes_seconds: Optional[int]) -> Optional[float]:
    """Convert stored minutes-in-seconds to decimal minutes (1 dp)."""
    if not minutes_seconds:
        return None
    return round(minutes_seconds / 60.0, 1)


def _pct(fraction: Optional[float]) -> Optional[float]:
    """Convert a 0-1 fraction to a 0-100 percentage, preserving ``None``."""
    if fraction is None:
        return None
    return round(fraction * 100.0, 1)


# --------------------------------------------------------------------------- #
# DTOs
# --------------------------------------------------------------------------- #


@dataclass
class GameRow:
    """One game summary row for the games index table."""

    game_id: int
    year: int
    venue: str
    venue_slug: str
    game_date: Optional[str]
    status: str
    data_quality: str
    home_name: str
    home_abbr: Optional[str]
    home_score: Optional[int]
    away_name: str
    away_abbr: Optional[str]
    away_score: Optional[int]


@dataclass
class GamesPage:
    """A paginated slice of the games index."""

    games: list[GameRow]
    total: int
    page: int
    page_size: int
    total_pages: int


@dataclass
class FacetOption:
    """One selectable filter value (venue or team)."""

    value: str
    label: str


@dataclass
class GamesFacets:
    """Available filter values for the games index controls."""

    years: list[int]
    venues: list[FacetOption]
    teams: list[FacetOption]


@dataclass
class BoxLine:
    """One player's box-score line within a game."""

    player_id: Optional[int]
    slug: Optional[str]
    name: str
    starter: bool
    dnp: bool
    minutes: Optional[float]
    pts: Optional[int]
    reb: Optional[int]
    ast: Optional[int]
    stl: Optional[int]
    blk: Optional[int]
    tov: Optional[int]
    pf: Optional[int]
    fg: str
    fg3: str
    ft: str
    plus_minus: Optional[int]
    ts_pct: Optional[float]
    efg_pct: Optional[float]
    usg_pct: Optional[float]


@dataclass
class TeamBox:
    """One team's side of a game box score."""

    team_entry_id: int
    name: str
    abbr: Optional[str]
    score: Optional[int]
    is_home: bool
    lines: list[BoxLine] = field(default_factory=list)
    totals: Optional[BoxLine] = None


@dataclass
class GameBox:
    """A complete game box score (header + both teams)."""

    game_id: int
    year: int
    venue: str
    game_date: Optional[str]
    status: str
    data_quality: str
    home: TeamBox
    away: TeamBox


@dataclass
class PlayerLogRow:
    """One game line on a player's game-log page."""

    game_id: int
    game_date: Optional[str]
    opponent: Optional[str]
    minutes: Optional[float]
    pts: Optional[int]
    reb: Optional[int]
    ast: Optional[int]
    stl: Optional[int]
    blk: Optional[int]
    tov: Optional[int]
    fg: str
    fg3: str
    ft: str
    plus_minus: Optional[int]


@dataclass
class PlayerLogSeason:
    """All of a player's games for one competition (year + venue)."""

    year: int
    venue: str
    label: str
    rows: list[PlayerLogRow]


@dataclass
class PlayerRef:
    """Minimal canonical-player identity for the games store routes."""

    id: int
    slug: Optional[str]
    name: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fg_str(made: Optional[int], att: Optional[int]) -> str:
    """Render a made-attempted shooting pair as ``"m-a"``."""
    return f"{made or 0}-{att or 0}"


def _enum_str(value: object) -> str:
    """Return a string-enum's ``.value`` (or ``str`` for a raw driver string)."""
    return value.value if hasattr(value, "value") else str(value)


# --------------------------------------------------------------------------- #
# Games index
# --------------------------------------------------------------------------- #


async def search_games(
    db: AsyncSession,
    *,
    year: Optional[int] = None,
    venue_slug: Optional[str] = None,
    team_slug: Optional[str] = None,
    player_id: Optional[int] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> GamesPage:
    """Return a filtered, paginated page of Summer League games (newest first).

    Args:
        db: Async database session.
        year: Restrict to a single competition year.
        venue_slug: Restrict to a single venue.
        team_slug: Games where either team's ``team_slug`` matches.
        player_id: Games a canonical player appeared in.
        page: 1-based page number.
        page_size: Rows per page.

    Returns:
        A :class:`GamesPage` slice plus pagination metadata.
    """
    page = max(1, page)
    page_size = max(1, page_size)

    home = aliased(SummerLeagueTeamEntry)
    away = aliased(SummerLeagueTeamEntry)
    game = SummerLeagueGame
    comp = SummerLeagueCompetition

    filters: list[Any] = []
    if year is not None:
        filters.append(comp.year == year)
    if venue_slug:
        filters.append(comp.venue_slug == venue_slug)
    if team_slug:
        filters.append(
            or_(
                home.team_slug == team_slug,  # type: ignore[arg-type]
                away.team_slug == team_slug,  # type: ignore[arg-type]
            )
        )
    if player_id is not None:
        appeared = select(SummerLeaguePlayerGameLog.game_id).where(  # type: ignore[call-overload]
            SummerLeaguePlayerGameLog.player_id == player_id  # type: ignore[arg-type]
        )
        filters.append(game.id.in_(appeared))  # type: ignore[union-attr]

    base = (
        select(
            game.id,
            comp.year,
            comp.venue_slug,
            game.game_date,
            game.status,
            game.source_quality,
            game.home_score,
            game.away_score,
            home.raw_team_name.label("home_name"),  # type: ignore[attr-defined]
            home.raw_team_abbreviation.label("home_abbr"),  # type: ignore[union-attr]
            away.raw_team_name.label("away_name"),  # type: ignore[attr-defined]
            away.raw_team_abbreviation.label("away_abbr"),  # type: ignore[union-attr]
        )  # type: ignore[call-overload, misc]
        .select_from(game)
        .join(comp, comp.id == game.competition_id)
        .join(home, home.id == game.home_team_entry_id, isouter=True)
        .join(away, away.id == game.away_team_entry_id, isouter=True)
    )
    if filters:
        base = base.where(*filters)

    count_stmt = select(func.count()).select_from(base.subquery())
    total = int((await db.execute(count_stmt)).scalar_one())

    rows_stmt = (
        base.order_by(
            desc(game.game_date),  # type: ignore[arg-type]
            desc(game.id),  # type: ignore[arg-type]
        )
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    result = await db.execute(rows_stmt)

    games: list[GameRow] = []
    for r in result.all():
        games.append(
            GameRow(
                game_id=r.id,
                year=r.year,
                venue=_venue_label(r.venue_slug),
                venue_slug=r.venue_slug,
                game_date=r.game_date.isoformat() if r.game_date else None,
                status=_enum_str(r.status),
                data_quality=_enum_str(r.source_quality),
                home_name=r.home_name or "—",
                home_abbr=r.home_abbr,
                home_score=r.home_score,
                away_name=r.away_name or "—",
                away_abbr=r.away_abbr,
                away_score=r.away_score,
            )
        )

    total_pages = max(1, (total + page_size - 1) // page_size)
    return GamesPage(
        games=games,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


async def get_games_facets(db: AsyncSession) -> GamesFacets:
    """Return distinct years, venues, and teams for the index filter controls."""
    year_rows = await db.execute(
        select(SummerLeagueCompetition.year)  # type: ignore[call-overload, misc]
        .distinct()
        .order_by(desc(SummerLeagueCompetition.year))  # type: ignore[arg-type]
    )
    years = [int(y) for (y,) in year_rows.all()]

    venue_rows = await db.execute(
        select(SummerLeagueCompetition.venue_slug).distinct()  # type: ignore[call-overload]
    )
    venue_slugs = [v for (v,) in venue_rows.all() if v]
    venues = [
        FacetOption(value=slug, label=_venue_label(slug))
        for slug in sorted(venue_slugs, key=lambda s: _VENUE_ORDER.get(s, 99))
    ]

    team_rows = await db.execute(
        select(
            SummerLeagueTeamEntry.team_slug,
            func.max(SummerLeagueTeamEntry.raw_team_name),
        )  # type: ignore[call-overload, misc]
        .group_by(SummerLeagueTeamEntry.team_slug)
        .order_by(func.max(SummerLeagueTeamEntry.raw_team_name))
    )
    teams = [
        FacetOption(value=slug, label=name or slug)
        for slug, name in team_rows.all()
        if slug
    ]

    return GamesFacets(years=years, venues=venues, teams=teams)


# --------------------------------------------------------------------------- #
# Single game box score
# --------------------------------------------------------------------------- #


def _box_line(r: object) -> BoxLine:
    """Build a :class:`BoxLine` from a joined player-game-log row."""
    minutes_seconds = getattr(r, "minutes_seconds", None)
    pts = getattr(r, "pts", None)
    dnp = (minutes_seconds or 0) <= 0
    return BoxLine(
        player_id=getattr(r, "player_id", None),
        slug=getattr(r, "slug", None),
        name=getattr(r, "raw_player_name", None) or "—",
        starter=bool(getattr(r, "starter_position", None)),
        dnp=dnp,
        minutes=_minutes(minutes_seconds),
        pts=pts,
        reb=getattr(r, "reb", None),
        ast=getattr(r, "ast", None),
        stl=getattr(r, "stl", None),
        blk=getattr(r, "blk", None),
        tov=getattr(r, "tov", None),
        pf=getattr(r, "pf", None),
        fg=_fg_str(getattr(r, "fgm", None), getattr(r, "fga", None)),
        fg3=_fg_str(getattr(r, "fg3m", None), getattr(r, "fg3a", None)),
        ft=_fg_str(getattr(r, "ftm", None), getattr(r, "fta", None)),
        plus_minus=getattr(r, "plus_minus", None),
        ts_pct=_pct(getattr(r, "ts_pct", None)),
        efg_pct=_pct(getattr(r, "efg_pct", None)),
        usg_pct=_pct(getattr(r, "usg_pct", None)),
    )


async def get_game_box_score(db: AsyncSession, game_id: int) -> Optional[GameBox]:
    """Return the full box score for one game, or ``None`` when not found.

    Args:
        db: Async database session.
        game_id: Internal ``summer_league_games.id``.

    Returns:
        A :class:`GameBox` with both teams' lines and totals, or ``None``.
    """
    home = aliased(SummerLeagueTeamEntry)
    away = aliased(SummerLeagueTeamEntry)
    game = SummerLeagueGame
    comp = SummerLeagueCompetition

    header_stmt = (
        select(
            game.id,
            comp.year,
            comp.venue_slug,
            game.game_date,
            game.status,
            game.source_quality,
            game.home_team_entry_id,
            game.away_team_entry_id,
            game.home_score,
            game.away_score,
            home.raw_team_name.label("home_name"),  # type: ignore[attr-defined]
            home.raw_team_abbreviation.label("home_abbr"),  # type: ignore[union-attr]
            away.raw_team_name.label("away_name"),  # type: ignore[attr-defined]
            away.raw_team_abbreviation.label("away_abbr"),  # type: ignore[union-attr]
        )  # type: ignore[call-overload, misc]
        .select_from(game)
        .join(comp, comp.id == game.competition_id)
        .join(home, home.id == game.home_team_entry_id, isouter=True)
        .join(away, away.id == game.away_team_entry_id, isouter=True)
        .where(game.id == game_id)  # type: ignore[arg-type]
    )
    header = (await db.execute(header_stmt)).first()
    if header is None:
        return None

    home_box = TeamBox(
        team_entry_id=header.home_team_entry_id,
        name=header.home_name or "Home",
        abbr=header.home_abbr,
        score=header.home_score,
        is_home=True,
    )
    away_box = TeamBox(
        team_entry_id=header.away_team_entry_id,
        name=header.away_name or "Away",
        abbr=header.away_abbr,
        score=header.away_score,
        is_home=False,
    )

    pgl = SummerLeaguePlayerGameLog
    lines_stmt = (
        select(
            pgl.team_entry_id,
            pgl.player_id,
            PlayerMaster.slug,
            pgl.raw_player_name,
            pgl.starter_position,
            pgl.minutes_seconds,
            pgl.pts,
            pgl.reb,
            pgl.ast,
            pgl.stl,
            pgl.blk,
            pgl.tov,
            pgl.pf,
            pgl.fgm,
            pgl.fga,
            pgl.fg3m,
            pgl.fg3a,
            pgl.ftm,
            pgl.fta,
            pgl.plus_minus,
            pgl.ts_pct,
            pgl.efg_pct,
            pgl.usg_pct,
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(PlayerMaster, PlayerMaster.id == pgl.player_id, isouter=True)
        .where(pgl.game_id == game_id)  # type: ignore[arg-type]
        .order_by(
            desc(pgl.minutes_seconds),  # type: ignore[arg-type]
            desc(pgl.pts),  # type: ignore[arg-type]
        )
    )
    for r in (await db.execute(lines_stmt)).all():
        line = _box_line(r)
        if r.team_entry_id == home_box.team_entry_id:
            home_box.lines.append(line)
        elif r.team_entry_id == away_box.team_entry_id:
            away_box.lines.append(line)

    tgl = SummerLeagueTeamGameLog
    totals_stmt = select(  # type: ignore[call-overload, misc]
        tgl.team_entry_id,
        tgl.minutes,
        tgl.pts,
        tgl.reb,
        tgl.ast,
        tgl.stl,
        tgl.blk,
        tgl.tov,
        tgl.pf,
        tgl.fgm,
        tgl.fga,
        tgl.fg3m,
        tgl.fg3a,
        tgl.ftm,
        tgl.fta,
        tgl.plus_minus,
        tgl.ts_pct,
        tgl.efg_pct,
    ).where(tgl.game_id == game_id)  # type: ignore[call-overload, arg-type]
    for r in (await db.execute(totals_stmt)).all():
        totals = BoxLine(
            player_id=None,
            slug=None,
            name="Team",
            starter=False,
            dnp=False,
            minutes=float(r.minutes) if r.minutes is not None else None,
            pts=r.pts,
            reb=r.reb,
            ast=r.ast,
            stl=r.stl,
            blk=r.blk,
            tov=r.tov,
            pf=r.pf,
            fg=_fg_str(r.fgm, r.fga),
            fg3=_fg_str(r.fg3m, r.fg3a),
            ft=_fg_str(r.ftm, r.fta),
            plus_minus=r.plus_minus,
            ts_pct=_pct(r.ts_pct),
            efg_pct=_pct(r.efg_pct),
            usg_pct=None,
        )
        if r.team_entry_id == home_box.team_entry_id:
            home_box.totals = totals
        elif r.team_entry_id == away_box.team_entry_id:
            away_box.totals = totals

    return GameBox(
        game_id=header.id,
        year=header.year,
        venue=_venue_label(header.venue_slug),
        game_date=header.game_date.isoformat() if header.game_date else None,
        status=_enum_str(header.status),
        data_quality=_enum_str(header.source_quality),
        home=home_box,
        away=away_box,
    )


# --------------------------------------------------------------------------- #
# Player game logs
# --------------------------------------------------------------------------- #


async def resolve_player_ref(db: AsyncSession, slug: str) -> Optional[PlayerRef]:
    """Resolve a slug to a minimal player ref in a single query.

    The games-store routes only need ``id``/``slug``/``name``; this avoids the
    heavier ``get_player_profile_by_slug`` (which also loads measurements and an
    image URL the routes discard).
    """
    row = (
        await db.execute(
            select(
                PlayerMaster.id,
                PlayerMaster.slug,
                PlayerMaster.display_name,
            )  # type: ignore[call-overload]
            .where(PlayerMaster.slug == slug)  # type: ignore[arg-type]
            .limit(1)
        )
    ).first()
    if row is None or row.id is None:
        return None
    return PlayerRef(id=row.id, slug=row.slug, name=row.display_name or "Player")


async def get_player_game_logs(
    db: AsyncSession, player_id: int
) -> list[PlayerLogSeason]:
    """Return a player's complete SL game logs grouped by competition (newest first).

    Args:
        db: Async database session.
        player_id: Canonical ``players_master`` id.

    Returns:
        A list of :class:`PlayerLogSeason`, each with its game rows ordered
        newest-first; empty when the player has no resolved logs.
    """
    opponent = aliased(SummerLeagueTeamEntry)
    pgl = SummerLeaguePlayerGameLog
    game = SummerLeagueGame
    comp = SummerLeagueCompetition

    stmt = (
        select(
            pgl.game_id,
            comp.year,
            comp.venue_slug,
            game.game_date,
            opponent.raw_team_abbreviation,
            opponent.raw_team_name,
            pgl.minutes_seconds,
            pgl.pts,
            pgl.reb,
            pgl.ast,
            pgl.stl,
            pgl.blk,
            pgl.tov,
            pgl.fgm,
            pgl.fga,
            pgl.fg3m,
            pgl.fg3a,
            pgl.ftm,
            pgl.fta,
            pgl.plus_minus,
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(game, game.id == pgl.game_id)
        .join(
            opponent,
            opponent.id  # type: ignore[arg-type]
            == case(
                (  # type: ignore[arg-type]
                    game.home_team_entry_id == pgl.team_entry_id,
                    game.away_team_entry_id,
                ),
                else_=game.home_team_entry_id,
            ),
            isouter=True,
        )
        .where(pgl.player_id == player_id)  # type: ignore[arg-type]
        .order_by(desc(game.game_date), desc(pgl.id))  # type: ignore[arg-type]
    )

    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    grouped: dict[tuple[int, str], list[PlayerLogRow]] = {}
    for r in rows:
        if (r.minutes_seconds or 0) <= 0:
            continue  # exclude DNPs from the per-game logs
        grouped.setdefault((r.year, r.venue_slug), []).append(
            PlayerLogRow(
                game_id=r.game_id,
                game_date=r.game_date.isoformat() if r.game_date else None,
                opponent=r.raw_team_abbreviation or r.raw_team_name,
                minutes=_minutes(r.minutes_seconds),
                pts=r.pts,
                reb=r.reb,
                ast=r.ast,
                stl=r.stl,
                blk=r.blk,
                tov=r.tov,
                fg=_fg_str(r.fgm, r.fga),
                fg3=_fg_str(r.fg3m, r.fg3a),
                ft=_fg_str(r.ftm, r.fta),
                plus_minus=r.plus_minus,
            )
        )

    ordered_keys = sorted(grouped, key=lambda k: (-k[0], _VENUE_ORDER.get(k[1], 99)))
    seasons: list[PlayerLogSeason] = []
    for year, venue_slug in ordered_keys:
        venue = _venue_label(venue_slug)
        seasons.append(
            PlayerLogSeason(
                year=year,
                venue=venue,
                label=f"{year} · {venue}",
                rows=grouped[(year, venue_slug)],
            )
        )
    return seasons

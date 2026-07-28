"""Read-side service for a player's Summer League production.

Aggregates normalized Summer League player box-score lines
(`summer_league_player_game_logs`) into per-year and career display stats for
the player-detail page's Summer League section.

This is intentionally raw-data-only: per-game plus two lightweight rate
transforms (per-36 and per-100). Per-36 needs only minutes; per-100 uses NBA's
supplied on-court ``pace`` (possessions/48) so no possession model is required.
Per-100 is ``None`` for seasons whose games carry no ``pace`` (pre-2017).
No composite/derived metrics are computed here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy import case, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.constants import MINUTES_PER_GAME
from app.services.summer_league.metrics import game_score_from_row
from app.services.summer_league_shotchart_service import (
    get_player_shot_dots,
    get_player_shot_zones,
)

# Human-readable venue labels keyed by the competition ``venue_slug``.
VENUE_LABELS: dict[str, str] = {
    "las_vegas": "Las Vegas",
    "salt_lake_city": "Salt Lake City",
    "california_classic": "California Classic",
    "orlando": "Orlando",
}

# Compact venue tags used to disambiguate same-year competition tabs.
_VENUE_ABBR: dict[str, str] = {
    "las_vegas": "LV",
    "salt_lake_city": "SLC",
    "california_classic": "CC",
    "orlando": "ORL",
}

# Display order within a year (marquee Las Vegas first).
_VENUE_ORDER: dict[str, int] = {
    "las_vegas": 0,
    "salt_lake_city": 1,
    "california_classic": 2,
    "orlando": 3,
}

# Counting stats that scale with the per-36 / per-100 rate transforms.
_RATE_STATS = ("pts", "reb", "ast", "stl", "blk", "tov")
# All box-score totals we sum per span. oreb/dreb/pf are not displayed as their
# own columns but are summed so Game Score (which weights them) can be derived.
_SUM_STATS = _RATE_STATS + (
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "pf",
)

_MINUTES_PER_GAME = MINUTES_PER_GAME
_RECENT_GAME_LIMIT = 5


@dataclass
class SummerLeagueModeStats:
    """Counting stats for one display mode (per-game / per-36 / per-100).

    Shooting makes/attempts are mode-scaled alongside the counting stats so the
    per-36 / per-100 views convey shot volume and free-throw rate, not just the
    percentages (which are span-constant and live on the season).
    """

    mode: str  # "per_game" | "per_36" | "per_100"
    pts: Optional[float]
    reb: Optional[float]
    ast: Optional[float]
    stl: Optional[float]
    blk: Optional[float]
    tov: Optional[float]
    fgm: Optional[float]
    fga: Optional[float]
    fg3m: Optional[float]
    fg3a: Optional[float]
    ftm: Optional[float]
    fta: Optional[float]


@dataclass
class SummerLeagueGameLine:
    """One recent Summer League game line for the mini-table."""

    game_id: Optional[int]
    game_date: Optional[str]
    year: int
    venue: str
    opponent: Optional[str]
    minutes: Optional[float]
    pts: Optional[int]
    reb: Optional[int]
    ast: Optional[int]
    fgm: Optional[int]
    fga: Optional[int]
    fg3m: Optional[int]
    fg3a: Optional[int]
    ftm: Optional[int]
    fta: Optional[int]
    # Hollinger Game Score for this single game.
    gmsc: Optional[float] = None


@dataclass
class SummerLeagueSeason:
    """Aggregated Summer League production for one year (across venues) or career."""

    year: Optional[int]
    season_label: str
    venues: list[str]
    gp: int
    gs: int
    mpg: Optional[float]
    total_minutes: float
    total_possessions: Optional[float]
    # Shooting percentages on a 0-100 scale (weighted from summed makes/attempts).
    fg_pct: Optional[float]
    fg3_pct: Optional[float]
    ft_pct: Optional[float]
    ts_pct: Optional[float]
    fg3a_per_g: Optional[float]
    fta_per_g: Optional[float]
    # Per-game average Hollinger Game Score across the span.
    gmsc: Optional[float]
    modes: dict[str, SummerLeagueModeStats]
    # Venue/league abbreviation (LV/SLC/CC/ORL) for a competition row; None for Career.
    venue_abbr: Optional[str] = None
    # Canonical venue slug for a competition row (used to target the per-season
    # page at the exact competition); None for Career.
    venue_slug: Optional[str] = None


@dataclass
class SummerLeagueProfile:
    """A player's full Summer League profile for the player-detail section."""

    seasons: list[SummerLeagueSeason]  # descending year
    career: SummerLeagueSeason
    recent_games: list[SummerLeagueGameLine]
    has_per100: bool


def _safe_div(num: float, den: float) -> Optional[float]:
    """Return ``num / den`` as a float, or ``None`` when ``den`` is zero."""
    if not den:
        return None
    return num / den


def _aggregate_season(
    rows: list,
    *,
    year: Optional[int],
    season_label: str,
) -> Optional[SummerLeagueSeason]:
    """Aggregate played game rows into a season/career line.

    Returns ``None`` when no row has minutes (e.g. all DNP), so the caller can
    suppress the span entirely.
    """
    played = [r for r in rows if (r.minutes_seconds or 0) > 0]
    gp = len(played)
    if gp == 0:
        return None

    gs = sum(1 for r in played if r.starter_position)
    total_minutes = sum((r.minutes_seconds or 0) for r in played) / 60.0
    sums = {
        stat: float(sum((getattr(r, stat) or 0) for r in played)) for stat in _SUM_STATS
    }

    # Possessions from NBA-supplied on-court pace; absent before 2017.
    total_possessions: Optional[float] = None
    poss_acc = 0.0
    for r in played:
        if r.pace and r.minutes_seconds:
            poss_acc += r.pace * (r.minutes_seconds / 60.0) / _MINUTES_PER_GAME
    if poss_acc > 0:
        total_possessions = poss_acc

    venues = sorted({VENUE_LABELS.get(r.venue_slug, r.venue_slug) for r in played})

    per_game_factor = 1.0 / gp
    per_36_factor = _safe_div(36.0, total_minutes)
    per_100_factor = (
        _safe_div(100.0, total_possessions) if total_possessions is not None else None
    )

    def _mode(name: str, factor: Optional[float]) -> SummerLeagueModeStats:
        if factor is None:
            return SummerLeagueModeStats(
                mode=name,
                pts=None,
                reb=None,
                ast=None,
                stl=None,
                blk=None,
                tov=None,
                fgm=None,
                fga=None,
                fg3m=None,
                fg3a=None,
                ftm=None,
                fta=None,
            )
        return SummerLeagueModeStats(
            mode=name,
            pts=sums["pts"] * factor,
            reb=sums["reb"] * factor,
            ast=sums["ast"] * factor,
            stl=sums["stl"] * factor,
            blk=sums["blk"] * factor,
            tov=sums["tov"] * factor,
            fgm=sums["fgm"] * factor,
            fga=sums["fga"] * factor,
            fg3m=sums["fg3m"] * factor,
            fg3a=sums["fg3a"] * factor,
            ftm=sums["ftm"] * factor,
            fta=sums["fta"] * factor,
        )

    modes = {
        "per_game": _mode("per_game", per_game_factor),
        "per_36": _mode("per_36", per_36_factor),
        "per_100": _mode("per_100", per_100_factor),
    }

    fg_pct = _pct(_safe_div(sums["fgm"], sums["fga"]))
    fg3_pct = _pct(_safe_div(sums["fg3m"], sums["fg3a"]))
    ft_pct = _pct(_safe_div(sums["ftm"], sums["fta"]))
    ts_denom = 2.0 * (sums["fga"] + 0.44 * sums["fta"])
    ts_pct = _pct(_safe_div(sums["pts"], ts_denom))

    # Per-game average Game Score: Game Score is linear in the box stats, so the
    # mean per-game value equals game_score(summed box) / gp.
    gmsc = round(game_score_from_row(sums) / gp, 1)

    return SummerLeagueSeason(
        year=year,
        season_label=season_label,
        venues=venues,
        gp=gp,
        gs=gs,
        mpg=total_minutes / gp,
        total_minutes=total_minutes,
        total_possessions=total_possessions,
        fg_pct=fg_pct,
        fg3_pct=fg3_pct,
        ft_pct=ft_pct,
        ts_pct=ts_pct,
        fg3a_per_g=sums["fg3a"] / gp,
        fta_per_g=sums["fta"] / gp,
        gmsc=gmsc,
        modes=modes,
    )


def _pct(fraction: Optional[float]) -> Optional[float]:
    """Convert a 0-1 fraction to a 0-100 percentage, preserving ``None``."""
    if fraction is None:
        return None
    return fraction * 100.0


def _format_minutes(minutes_seconds: Optional[int]) -> Optional[float]:
    """Convert stored minutes-in-seconds to decimal minutes."""
    if not minutes_seconds:
        return None
    return round(minutes_seconds / 60.0, 1)


async def get_summer_league_profile_by_player_id(
    db: AsyncSession,
    player_id: int,
) -> Optional[SummerLeagueProfile]:
    """Fetch and aggregate a player's Summer League production.

    Args:
        db: Async database session.
        player_id: Canonical ``players_master`` id.

    Returns:
        A :class:`SummerLeagueProfile` with per-year + career stats and recent
        games, or ``None`` when the player has no resolved Summer League game
        logs (so the route can omit the section).
    """
    opponent = aliased(SummerLeagueTeamEntry)
    pgl = SummerLeaguePlayerGameLog

    stmt = (
        select(
            SummerLeagueGame.id.label("sl_game_id"),  # type: ignore[union-attr]
            SummerLeagueCompetition.year,
            SummerLeagueCompetition.venue_slug,
            SummerLeagueGame.game_date,
            opponent.raw_team_abbreviation,
            opponent.raw_team_name,
            pgl.minutes_seconds,
            pgl.starter_position,
            pgl.pace,
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
            pgl.oreb,
            pgl.dreb,
            pgl.pf,
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(SummerLeagueCompetition, SummerLeagueCompetition.id == pgl.competition_id)
        .join(SummerLeagueGame, SummerLeagueGame.id == pgl.game_id)
        .join(
            opponent,
            opponent.id  # type: ignore[arg-type]
            == case(
                (  # type: ignore[arg-type]
                    SummerLeagueGame.home_team_entry_id == pgl.team_entry_id,
                    SummerLeagueGame.away_team_entry_id,
                ),
                else_=SummerLeagueGame.home_team_entry_id,
            ),
            isouter=True,
        )
        .where(pgl.player_id == player_id)  # type: ignore[arg-type]
        .order_by(desc(SummerLeagueGame.game_date), desc(pgl.id))  # type: ignore[arg-type]
    )

    result = await db.execute(stmt)
    rows = list(result.all())
    if not rows:
        return None

    # Group by competition (year + venue): California Classic / Salt Lake City
    # are distinct warm-up leagues that run before Las Vegas, so a player who
    # appears in two venues the same summer gets a row per competition. The
    # Career line still combines everything.
    rows_by_comp: dict[tuple[int, str], list] = {}
    for row in rows:
        rows_by_comp.setdefault((row.year, row.venue_slug), []).append(row)

    # Newest year first; within a year, marquee Las Vegas first.
    ordered_keys = sorted(
        rows_by_comp,
        key=lambda k: (-k[0], _VENUE_ORDER.get(k[1], 99)),
    )
    seasons: list[SummerLeagueSeason] = []
    for year, venue_slug in ordered_keys:
        season = _aggregate_season(
            rows_by_comp[(year, venue_slug)], year=year, season_label=str(year)
        )
        if season is not None:
            season.venue_abbr = _VENUE_ABBR.get(venue_slug, venue_slug)
            season.venue_slug = venue_slug
            seasons.append(season)

    if not seasons:
        return None

    career = _aggregate_season(rows, year=None, season_label="Career")
    assert career is not None  # at least one season survived above

    recent_games: list[SummerLeagueGameLine] = []
    for row in rows:
        if (row.minutes_seconds or 0) <= 0:
            continue
        recent_games.append(
            SummerLeagueGameLine(
                game_id=row.sl_game_id,
                game_date=row.game_date.isoformat() if row.game_date else None,
                year=row.year,
                venue=VENUE_LABELS.get(row.venue_slug, row.venue_slug),
                opponent=row.raw_team_abbreviation or row.raw_team_name,
                minutes=_format_minutes(row.minutes_seconds),
                pts=row.pts,
                reb=row.reb,
                ast=row.ast,
                fgm=row.fgm,
                fga=row.fga,
                fg3m=row.fg3m,
                fg3a=row.fg3a,
                ftm=row.ftm,
                fta=row.fta,
                gmsc=round(game_score_from_row(row), 1),
            )
        )
        if len(recent_games) >= _RECENT_GAME_LIMIT:
            break

    return SummerLeagueProfile(
        seasons=seasons,
        career=career,
        recent_games=recent_games,
        has_per100=any(s.total_possessions is not None for s in seasons),
    )


def summer_league_to_context(profile: SummerLeagueProfile) -> dict:
    """Serialize a profile to a JSON-able dict for the template and window data."""
    return asdict(profile)


async def get_competition_id_for_player_year(
    db: AsyncSession,
    player_id: int,
    year: int,
    venue_slug: Optional[str] = None,
) -> Optional[int]:
    """Return a competition_id for a player in a given year.

    When ``venue_slug`` is given, return that exact competition (so a clicked
    Salt Lake / California Classic row opens its own shot chart rather than the
    marquee one). Otherwise the most-marquee competition is chosen using the
    ordering in ``_VENUE_ORDER`` (Las Vegas > Salt Lake City > California
    Classic > Orlando).  Returns ``None`` when the player has no
    :class:`~app.schemas.summer_league_metrics.SummerLeaguePlayerSeason` row
    for that year — i.e. the season table has not been materialised yet or the
    player had no resolved logs.

    Args:
        db: Async database session.
        player_id: Canonical ``players_master`` id.
        year: Summer League year to look up.
        venue_slug: Optional competition venue to target exactly; when ``None``
            the most-marquee competition for the year is returned.

    Returns:
        The resolved competition id, or ``None``.
    """
    stmt = select(  # type: ignore[call-overload]
        SummerLeaguePlayerSeason.competition_id,
        SummerLeaguePlayerSeason.venue_slug,
    ).where(
        SummerLeaguePlayerSeason.player_id == player_id,  # type: ignore[arg-type]
        SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.year == year,  # type: ignore[arg-type]
    )
    if venue_slug is not None:
        stmt = stmt.where(
            SummerLeaguePlayerSeason.venue_slug == venue_slug  # type: ignore[arg-type]
        )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None
    rows_sorted = sorted(rows, key=lambda r: _VENUE_ORDER.get(r.venue_slug, 99))
    return int(rows_sorted[0].competition_id)


async def get_player_shotchart_context(
    db: AsyncSession,
    player_id: int,
    competition_id: Optional[int] = None,
) -> Optional[dict]:
    """Assemble the ``window.SL_SHOTCHART`` payload for player templates.

    Returns ``None`` when the player has no shot events so callers can skip
    rendering the chart component entirely.

    When ``competition_id`` is ``None`` a career-level zone rollup is produced:
    zone counts are summed across all competitions and ``pool_fg_pct`` is
    ``None`` on every zone (no single reference pool, so the heat falls back to
    a sequential FG% scale).  ``dots`` still spans the player's whole career —
    shot locations share one court system, so the heat map renders correctly.
    The shot-diet row comes from the most recent
    :class:`SummerLeaguePlayerSeason` row for the player.

    When ``competition_id`` is provided the zones are scoped to that pool
    (``pool_fg_pct`` is populated), dots are fetched, and shot-diet comes from
    the matching ``SummerLeaguePlayerSeason`` row.

    Args:
        db: Async database session.
        player_id: Canonical ``players_master`` id.
        competition_id: Optional competition scope; ``None`` = career rollup.

    Returns:
        A JSON-serialisable dict shaped for ``window.SL_SHOTCHART``, or ``None``
        when the player has no shot events.
    """
    # ── 1. Zone aggregation (always fires; 1 query for career, 2 for scoped) ──
    zones_dto = await get_player_shot_zones(db, player_id, competition_id)
    if zones_dto.total_fga == 0:
        return None

    # ── 2. Shot-diet from SummerLeaguePlayerSeason ────────────────────────────
    if competition_id is not None:
        diet_stmt = select(SummerLeaguePlayerSeason).where(  # type: ignore[call-overload]
            SummerLeaguePlayerSeason.player_id == player_id,  # type: ignore[arg-type]
            SummerLeaguePlayerSeason.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
        )
    else:
        # Career view: use the most recent competition's rates as the "latest" diet.
        diet_stmt = (
            select(SummerLeaguePlayerSeason)  # type: ignore[call-overload]
            .where(
                SummerLeaguePlayerSeason.player_id == player_id,  # type: ignore[arg-type]
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
            .order_by(
                desc(SummerLeaguePlayerSeason.year),  # type: ignore[arg-type]
                SummerLeaguePlayerSeason.venue_slug,
            )
            .limit(1)
        )
    diet_row: Optional[SummerLeaguePlayerSeason] = (
        await db.execute(diet_stmt)
    ).scalar_one_or_none()

    shot_diet: Optional[dict] = None
    if diet_row is not None and any(
        v is not None
        for v in (
            diet_row.rim_rate,
            diet_row.mid_rate,
            diet_row.three_rate,
            diet_row.corner3_rate,
        )
    ):
        # Compute assisted-FG% when PBP counts are available for this row.
        ast_fgm = diet_row.ast_fgm
        unast_fgm = diet_row.unast_fgm
        total_pbp_fgm = (ast_fgm or 0) + (unast_fgm or 0)
        assisted_fg_pct: Optional[float] = (
            round(ast_fgm / total_pbp_fgm, 4)
            if ast_fgm is not None and unast_fgm is not None and total_pbp_fgm > 0
            else None
        )
        shot_diet = {
            "rim_rate": diet_row.rim_rate,
            "mid_rate": diet_row.mid_rate,
            "three_rate": diet_row.three_rate,
            "corner3_rate": diet_row.corner3_rate,
            "assisted_fg_pct": assisted_fg_pct,
        }

    # ── 3. Dots — career (all shots) or scoped to one competition ────────────
    # Career still plots dots: shot locations share one court system, so the
    # heat map renders fine; only the vs-pool COLOURING is competition-specific
    # (career falls back to the sequential FG% scale).
    dots_dto = await get_player_shot_dots(db, player_id, competition_id)
    dots = [{"loc_x": d.loc_x, "loc_y": d.loc_y, "made": d.made} for d in dots_dto.dots]

    return {
        "total_fga": zones_dto.total_fga,
        "suppressed": zones_dto.suppressed,
        "zones": [
            {
                "shot_zone_basic": z.shot_zone_basic,
                "fga": z.fga,
                "fgm": z.fgm,
                "fg_pct": z.fg_pct,
                "freq_pct": z.freq_pct,
                "pool_fg_pct": z.pool_fg_pct,
            }
            for z in zones_dto.zones
        ],
        "dots": dots,
        "shot_diet": shot_diet,
    }

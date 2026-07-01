"""Read-side service for the Summer League leaders page.

A comprehensive, sortable leaderboard across five display modes:

* ``totals`` / ``per_game`` / ``per_36`` / ``per_100`` — counting stats from the
  box logs, scaled per the mode (per-100 uses NBA-supplied on-court ``pace``).
* ``advanced`` — SL-calibrated composites (PER, ratings, BPM, WS, VORP, …) read
  from the materialized ``summer_league_player_seasons`` table. Those metrics are
  recalibrated per pool, so the advanced board is scoped to a single competition
  (year + venue) rather than aggregated across the whole season.

Every visible column is sortable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.services.summer_league.constants import MINUTES_PER_GAME
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeaguePlayerGameLog,
)
from app.services.summer_league_metrics_service import (
    ADV_LEADER_COLUMNS,
    VENUE_LABELS,
    get_blended_leaders,
    get_competition_leaders,
)

DEFAULT_MIN_GAMES = 2
DEFAULT_MIN_MINUTES = 60
PAGE_SIZE = 25

_MINUTES_PER_GAME = MINUTES_PER_GAME

MODES = ("totals", "per_game", "per_36", "per_100", "advanced")
MODE_LABELS = {
    "totals": "Totals",
    "per_game": "Per Game",
    "per_36": "Per 36",
    "per_100": "Per 100",
    "advanced": "Advanced",
}

# Counting stats that scale with the per-game / per-36 / per-100 transforms.
# Includes split rebounds, fouls, shooting volume, and plus-minus so the rate
# modes convey a full box line, not just the headline counts and percentages.
_COUNTING = (
    "pts",
    "oreb",
    "dreb",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "plus_minus",
)

# Column header labels keyed by column key. Counting columns come from the box
# logs; advanced columns come from the materialized metrics table.
COLUMN_LABELS: dict[str, str] = {
    "min": "MIN",
    "pts": "PTS",
    "oreb": "OREB",
    "dreb": "DREB",
    "reb": "REB",
    "ast": "AST",
    "stl": "STL",
    "blk": "BLK",
    "tov": "TOV",
    "pf": "PF",
    "fgm": "FGM",
    "fga": "FGA",
    "fg3m": "3PM",
    "fg3a": "3PA",
    "ftm": "FTM",
    "fta": "FTA",
    "plus_minus": "+/-",
    "fg_pct": "FG%",
    "fg3_pct": "3P%",
    "ft_pct": "FT%",
    "efg_pct": "eFG%",
    "ts_pct": "TS%",
    "per": "PER",
    "usg_pct": "USG%",
    "ast_pct": "AST%",
    "orb_pct": "OREB%",
    "drb_pct": "DREB%",
    "trb_pct": "REB%",
    "stl_pct": "STL%",
    "blk_pct": "BLK%",
    "tov_pct": "TOV%",
    "ortg": "ORtg",
    "drtg": "DRtg",
    "obpm": "OBPM",
    "dbpm": "DBPM",
    "bpm": "BPM",
    "ows": "OWS",
    "dws": "DWS",
    "ws": "WS",
    "ws40": "WS/40",
    "vorp": "VORP",
    "gmsc": "GmSc",
}

# Display order for the counting modes: groups makes/attempts with their % and
# keeps plus-minus last, BBRef-style.
_COUNTING_COLUMNS = [
    "min",
    "pts",
    "oreb",
    "dreb",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "fgm",
    "fga",
    "fg_pct",
    "fg3m",
    "fg3a",
    "fg3_pct",
    "ftm",
    "fta",
    "ft_pct",
    "efg_pct",
    "ts_pct",
    "plus_minus",
]
# Advanced mode is per-competition and sourced from summer_league_player_seasons.
_ADVANCED_COLUMNS = list(ADV_LEADER_COLUMNS)
_ADVANCED_DEFAULT_SORT = "per"
MODE_COLUMNS: dict[str, list[str]] = {
    "totals": _COUNTING_COLUMNS,
    "per_game": _COUNTING_COLUMNS,
    "per_36": _COUNTING_COLUMNS,
    "per_100": _COUNTING_COLUMNS,
    "advanced": _ADVANCED_COLUMNS,
}


@dataclass
class LeaderColumn:
    """One leaderboard column header."""

    key: str
    label: str
    sortable: bool
    placeholder: bool


@dataclass
class LeaderRow:
    """One player's row: rank, identity, and a value per column key."""

    rank: int
    slug: Optional[str]
    name: str
    gp: int
    values: dict[str, Optional[float]] = field(default_factory=dict)


@dataclass
class LeadersResult:
    """A page of leaders plus the controls' resolved state."""

    mode: str
    year: Optional[int]
    sort: str
    direction: str
    min_games: int
    min_minutes: int
    years: list[int]
    columns: list[LeaderColumn]
    rows: list[LeaderRow]
    total: int
    page: int
    page_size: int
    total_pages: int
    # Advanced mode is scoped to a single competition; these drive the extra
    # venue picker and are unset for the counting modes.
    is_advanced: bool = False
    venue: Optional[str] = None
    venue_label: Optional[str] = None
    venues: list[tuple[str, str]] = field(default_factory=list)


def _safe_div(num: float, den: float) -> Optional[float]:
    return num / den if den else None


def _pct(fraction: Optional[float]) -> Optional[float]:
    return round(fraction * 100.0, 1) if fraction is not None else None


async def get_leaders_years(db: AsyncSession) -> list[int]:
    """Return Summer League years that have player logs, newest first."""
    rows = await db.execute(
        select(SummerLeagueCompetition.year)  # type: ignore[call-overload]
        .distinct()
        .order_by(SummerLeagueCompetition.year.desc())  # type: ignore[attr-defined]
    )
    return [int(y) for (y,) in rows.all()]


# Venue display order mirrors the marquee-first ordering of VENUE_LABELS.
_VENUE_ORDER_KEYS = list(VENUE_LABELS.keys())


async def get_leaders_venues(db: AsyncSession) -> list[tuple[str, str]]:
    """Return ``(venue_slug, label)`` for venues present in competitions.

    Ordered marquee-first (Las Vegas → Salt Lake City → California Classic →
    Orlando) so the counting-mode venue picker reads consistently with the rest
    of the Summer League surfaces.
    """
    rows = await db.execute(
        select(SummerLeagueCompetition.venue_slug).distinct()  # type: ignore[call-overload]
    )
    slugs = {v for (v,) in rows.all()}
    ordered = sorted(
        slugs,
        key=lambda s: _VENUE_ORDER_KEYS.index(s) if s in _VENUE_ORDER_KEYS else 99,
    )
    return [(s, VENUE_LABELS.get(s, s)) for s in ordered]


async def get_leaders(
    db: AsyncSession,
    *,
    mode: str = "per_game",
    year: Optional[int] = None,
    venue: Optional[str] = None,
    sort: Optional[str] = None,
    direction: str = "desc",
    min_games: int = DEFAULT_MIN_GAMES,
    min_minutes: int = DEFAULT_MIN_MINUTES,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> LeadersResult:
    """Aggregate and rank Summer League leaders for one mode/scope.

    Args:
        db: Async database session.
        mode: One of :data:`MODES`.
        year: Restrict to a single season; ``None`` for all-time/career.
        venue: Venue slug; only used by ``advanced`` mode to scope the
            competition (counting modes aggregate across venues).
        sort: Column key to rank by (defaults to the mode's headline stat).
        direction: ``"desc"`` (default) or ``"asc"``.
        min_games: Minimum games played to qualify.
        min_minutes: Minimum total minutes to qualify.
        page: 1-based page number.
        page_size: Rows per page.

    Returns:
        A :class:`LeadersResult` for rendering.
    """
    mode = mode if mode in MODES else "per_game"
    page = max(1, page)
    direction = "asc" if direction == "asc" else "desc"
    if mode == "advanced":
        return await _advanced_leaders(
            db,
            year=year,
            venue=venue,
            sort=sort,
            direction=direction,
            min_games=min_games,
            min_minutes=min_minutes,
            page=page,
            page_size=page_size,
        )

    columns_keys = MODE_COLUMNS[mode]
    if sort not in columns_keys:
        sort = "pts"

    # Venue is optional for the counting modes ("All venues" when unset); ignore
    # a slug that doesn't correspond to a real competition.
    venues = await get_leaders_venues(db)
    venue = venue if venue in {v[0] for v in venues} else None

    rows = await _aggregate(
        db, year=year, venue=venue, min_games=min_games, min_minutes=min_minutes
    )
    computed = [_compute_row(r, mode) for r in rows]

    reverse = direction == "desc"
    computed.sort(
        key=lambda row: (
            row.values.get(sort) is None,  # Nones last regardless of direction
            -(row.values.get(sort) or 0.0)
            if reverse
            else (row.values.get(sort) or 0.0),
        )
    )

    total = len(computed)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    page_rows = computed[start : start + page_size]
    for i, row in enumerate(page_rows, start=start + 1):
        row.rank = i

    columns = [
        LeaderColumn(key=k, label=COLUMN_LABELS[k], sortable=True, placeholder=False)
        for k in columns_keys
    ]
    return LeadersResult(
        mode=mode,
        year=year,
        sort=sort,
        direction=direction,
        min_games=min_games,
        min_minutes=min_minutes,
        years=await get_leaders_years(db),
        columns=columns,
        rows=page_rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        venue=venue,
        venue_label=VENUE_LABELS.get(venue) if venue else None,
        venues=venues,
    )


async def _advanced_leaders(
    db: AsyncSession,
    *,
    year: Optional[int],
    venue: Optional[str],
    sort: Optional[str],
    direction: str,
    min_games: int,
    min_minutes: int,
    page: int,
    page_size: int,
) -> LeadersResult:
    """Rank players by SL-calibrated advanced metrics for one scope.

    Reads the materialized ``summer_league_player_seasons`` table via the metrics
    service. With both a season and a venue chosen this ranks within that single
    (pool-recalibrated) competition; leaving either open blends a player's
    adv-eligible pools into one line — up to the all-time, all-venue career board.
    """
    if year is not None and venue is not None:
        comp = await get_competition_leaders(
            db,
            year=year,
            venue_slug=venue,
            min_games=min_games,
            min_minutes=min_minutes,
        )
    else:
        comp = await get_blended_leaders(
            db,
            year=year,
            venue_slug=venue,
            min_games=min_games,
            min_minutes=min_minutes,
        )
    if sort not in _ADVANCED_COLUMNS:
        sort = _ADVANCED_DEFAULT_SORT
    reverse = direction == "desc"

    rows = [
        LeaderRow(rank=0, slug=r.slug, name=r.name, gp=r.gp, values=r.values)
        for r in comp.rows
    ]
    rows.sort(
        key=lambda row: (
            row.values.get(sort) is None,  # Nones last regardless of direction
            -(row.values.get(sort) or 0.0)
            if reverse
            else (row.values.get(sort) or 0.0),
        )
    )

    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]
    for i, row in enumerate(page_rows, start=start + 1):
        row.rank = i

    columns = [
        LeaderColumn(key=k, label=COLUMN_LABELS[k], sortable=True, placeholder=False)
        for k in _ADVANCED_COLUMNS
    ]
    adv_years = sorted({y for (y, _v) in comp.competitions}, reverse=True)
    seen: set[str] = set()
    venues: list[tuple[str, str]] = []
    for _y, v in comp.competitions:
        if v not in seen:
            seen.add(v)
            venues.append((v, VENUE_LABELS.get(v, v)))
    return LeadersResult(
        mode="advanced",
        year=comp.year,
        sort=sort,
        direction=direction,
        min_games=min_games,
        min_minutes=min_minutes,
        years=adv_years,
        columns=columns,
        rows=page_rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        is_advanced=True,
        venue=comp.venue_slug,
        venue_label=comp.venue_label,
        venues=venues,
    )


async def _aggregate(
    db: AsyncSession,
    *,
    year: Optional[int],
    venue: Optional[str],
    min_games: int,
    min_minutes: int,
) -> list[Any]:
    """Aggregate per-player box totals for the counting display modes."""
    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition
    sec: Any = pgl.minutes_seconds  # treat as a column expression for arithmetic

    conds: list[Any] = [
        pgl.player_id.isnot(None),  # type: ignore[union-attr]
        sec > 0,
    ]
    if year is not None:
        conds.append(comp.year == year)  # type: ignore[arg-type]
    if venue:
        conds.append(comp.venue_slug == venue)  # type: ignore[arg-type]

    stmt = (
        select(
            pgl.player_id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            func.count().label("gp"),
            func.sum(sec).label("sec"),
            func.sum(pgl.pace * sec).label("pace_sec"),
            func.sum(pgl.pts).label("pts"),
            func.sum(pgl.oreb).label("oreb"),
            func.sum(pgl.dreb).label("dreb"),
            func.sum(pgl.reb).label("reb"),
            func.sum(pgl.ast).label("ast"),
            func.sum(pgl.stl).label("stl"),
            func.sum(pgl.blk).label("blk"),
            func.sum(pgl.tov).label("tov"),
            func.sum(pgl.pf).label("pf"),
            func.sum(pgl.plus_minus).label("plus_minus"),
            func.sum(pgl.fgm).label("fgm"),
            func.sum(pgl.fga).label("fga"),
            func.sum(pgl.fg3m).label("fg3m"),
            func.sum(pgl.fg3a).label("fg3a"),
            func.sum(pgl.ftm).label("ftm"),
            func.sum(pgl.fta).label("fta"),
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(PlayerMaster, PlayerMaster.id == pgl.player_id)
        .where(*conds)
        .group_by(pgl.player_id, PlayerMaster.slug, PlayerMaster.display_name)
        .having(func.count() >= min_games)
        .having(func.sum(sec) >= min_minutes * 60)
    )
    return list((await db.execute(stmt)).all())


def _compute_row(r: Any, mode: str) -> LeaderRow:
    """Compute one player's column values for the selected mode."""
    gp = int(r.gp)
    sec = float(r.sec or 0)
    minutes = sec / 60.0
    poss = (r.pace_sec or 0) / (60.0 * _MINUTES_PER_GAME)

    if mode == "per_game":
        factor = _safe_div(1.0, gp)
        min_val: Optional[float] = round(minutes / gp, 1) if gp else None
    elif mode == "per_36":
        factor = _safe_div(36.0, minutes)
        min_val = round(minutes / gp, 1) if gp else None
    elif mode == "per_100":
        factor = _safe_div(100.0, poss) if poss else None
        min_val = round(minutes / gp, 1) if gp else None
    else:  # totals
        factor = 1.0
        min_val = round(minutes, 1)

    def scaled(total: Optional[float]) -> Optional[float]:
        if factor is None or total is None:
            return None
        v = total * factor
        return round(v) if mode == "totals" else round(v, 1)

    fga, fta = float(r.fga or 0), float(r.fta or 0)
    fgm, fg3m = float(r.fgm or 0), float(r.fg3m or 0)
    values: dict[str, Optional[float]] = {
        "min": min_val,
        **{c: scaled(float(getattr(r, c) or 0)) for c in _COUNTING},
        "fg_pct": _pct(_safe_div(fgm, fga)),
        "fg3_pct": _pct(_safe_div(fg3m, float(r.fg3a or 0))),
        "ft_pct": _pct(_safe_div(float(r.ftm or 0), fta)),
        "efg_pct": _pct(_safe_div(fgm + 0.5 * fg3m, fga)),
        "ts_pct": _pct(_safe_div(float(r.pts or 0), 2.0 * (fga + 0.44 * fta))),
    }
    return LeaderRow(
        rank=0, slug=r.slug, name=r.display_name or "Player", gp=gp, values=values
    )

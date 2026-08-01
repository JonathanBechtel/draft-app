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

# discipline: file-size Phase 2 formula wiring; no new scope in ticket #745

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.services.stats.formulas import efg_pct_line, ts_pct_line
from app.services.summer_league.constants import MINUTES_PER_GAME
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeaguePlayerGameLog,
)
from app.services.stats.scaling import scale_python
from app.services.summer_league_metrics_service import (
    ADV_LEADER_COLUMNS,
    VENUE_LABELS,
    get_blended_leaders,
    get_competition_leaders,
)

DEFAULT_MIN_GAMES = 2
DEFAULT_MIN_MINUTES = 60
PAGE_SIZE = 25

# When the caller doesn't pin explicit thresholds, qualification adapts to the
# data: try the standard gate first, then relax rung by rung until the board
# reads as a real leaderboard. Keeps early-competition boards (day 1-2 of a
# venue) and small venues from rendering empty or three-rows-thin while leaving
# mature scopes on the standard gate. Users can always tighten via the
# Min GP / Min MIN filters.
GATE_LADDER: tuple[tuple[int, int], ...] = (
    (DEFAULT_MIN_GAMES, DEFAULT_MIN_MINUTES),
    (1, 20),
    (1, 0),
)

# A rung "populates" the board once it shows at least this many players; fewer
# and the ladder keeps relaxing (stopping at the floor rung regardless). A
# board smaller than this reads as broken/unsatisfying rather than exclusive.
TARGET_BOARD_ROWS = 10

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
    "fg3ar": "3PAr",
    "ftr": "FTr",
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
    # Gate provenance: ``auto_gates`` means the caller didn't pin thresholds
    # (links/forms should stay adaptive); ``gates_relaxed`` means the standard
    # gate matched nobody and a lower rung populated the board.
    auto_gates: bool = False
    gates_relaxed: bool = False
    # The standard (strictest) rung, exposed so UI copy stays in sync with it.
    standard_min_games: int = DEFAULT_MIN_GAMES
    standard_min_minutes: int = DEFAULT_MIN_MINUTES
    # Advanced mode only: the resolved pool hasn't earned adv-eligibility yet,
    # so only box-derived rate columns carry values.
    uncalibrated: bool = False


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
    min_games: Optional[int] = None,
    min_minutes: Optional[int] = None,
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
        min_games: Minimum games played to qualify; ``None`` (with
            ``min_minutes`` also ``None``) applies :data:`GATE_LADDER`
            adaptively so a scope with any data never renders empty.
        min_minutes: Minimum total minutes to qualify; ``None`` as above.
        page: 1-based page number.
        page_size: Rows per page.

    Returns:
        A :class:`LeadersResult` for rendering.
    """
    mode = mode if mode in MODES else "per_game"
    page = max(1, page)
    direction = "asc" if direction == "asc" else "desc"

    auto_gates = min_games is None and min_minutes is None
    gates: tuple[tuple[int, int], ...]
    if auto_gates:
        gates = GATE_LADDER
    else:
        gates = (
            (
                DEFAULT_MIN_GAMES if min_games is None else min_games,
                DEFAULT_MIN_MINUTES if min_minutes is None else min_minutes,
            ),
        )

    if mode == "advanced":
        return await _advanced_leaders(
            db,
            year=year,
            venue=venue,
            sort=sort,
            direction=direction,
            gates=gates,
            auto_gates=auto_gates,
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

    # Gates live in the aggregate's HAVING clause (scopes can span thousands of
    # players, so rows are never fetched ungated); each under-target rung
    # re-runs it.
    for applied_games, applied_minutes in gates:
        rows = await _aggregate(
            db,
            year=year,
            venue=venue,
            min_games=applied_games,
            min_minutes=applied_minutes,
        )
        if len(rows) >= TARGET_BOARD_ROWS:
            break
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
        min_games=applied_games,
        min_minutes=applied_minutes,
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
        auto_gates=auto_gates,
        gates_relaxed=bool(rows) and (applied_games, applied_minutes) != gates[0],
    )


async def _advanced_leaders(
    db: AsyncSession,
    *,
    year: Optional[int],
    venue: Optional[str],
    sort: Optional[str],
    direction: str,
    gates: tuple[tuple[int, int], ...],
    auto_gates: bool,
    page: int,
    page_size: int,
) -> LeadersResult:
    """Rank players by SL-calibrated advanced metrics for one scope.

    Reads the materialized ``summer_league_player_seasons`` table via the metrics
    service. With both a season and a venue chosen this ranks within that single
    (pool-recalibrated) competition; leaving either open blends a player's
    adv-eligible pools into one line — up to the all-time, all-venue career board.

    The fetcher walks ``gates`` rung by rung over its once-fetched scope (a
    single rung when the caller pinned explicit thresholds) and reports the
    gate it actually enforced. A resolved pool that isn't adv-eligible yet
    ranks by GmSc, since the calibrated composites are unset.
    """
    if year is not None and venue is not None:
        comp = await get_competition_leaders(
            db,
            year=year,
            venue_slug=venue,
            gates=gates,
            min_rows=TARGET_BOARD_ROWS,
        )
    else:
        comp = await get_blended_leaders(
            db,
            year=year,
            venue_slug=venue,
            gates=gates,
            min_rows=TARGET_BOARD_ROWS,
        )
    # Auto gates report the enforced rung (incl. the fetcher's minutes floor);
    # pinned gates echo the request so the filter inputs stay as typed.
    min_games, min_minutes = (
        (comp.min_games, comp.min_minutes) if auto_gates else gates[0]
    )
    if sort not in _ADVANCED_COLUMNS:
        # Uncalibrated pools carry no PER; GmSc is the box-derived headline.
        sort = _ADVANCED_DEFAULT_SORT if comp.calibrated else "gmsc"
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
    # ``comp.competitions`` already includes the resolved pool (even when
    # uncalibrated), so the pickers derive directly from it.
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
        auto_gates=auto_gates,
        gates_relaxed=auto_gates
        and bool(comp.rows)
        and (comp.min_games, comp.min_minutes) != gates[0],
        uncalibrated=not comp.calibrated,
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

    # per_100's denominator is pace-derived possessions, but ``pace`` is NULL for
    # pace-gap games (pre-2017 pools reconstructed without team box data). Summing
    # only the pace-covered possessions while the counting-stat numerators cover
    # *every* game inflates per-100 by ``total_min / pace_covered_min`` for any
    # player whose career straddles the boundary. Extrapolate possessions to all
    # minutes with the minute-weighted observed pace so numerator and denominator
    # span the same games — mirroring the Explorer's ``pace_sec_expr``:
    #   pace_sec = SUM(pace × sec) × SUM(sec) / SUM(sec where pace > 0).
    # Complete coverage leaves this exact; no pace-covered games → NULL → per-100
    # renders blank rather than a garbage rate.
    paced_sec = func.sum(case((pgl.pace > 0, sec), else_=literal(0)))  # type: ignore[arg-type, operator]
    pace_sec_expr = func.sum(pgl.pace * sec) * func.sum(sec) / func.nullif(paced_sec, 0)

    stmt = (
        select(
            pgl.player_id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            func.count().label("gp"),
            func.sum(sec).label("sec"),
            pace_sec_expr.label("pace_sec"),
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
    pace_seconds = float(r.pace_sec or 0)
    factor = scale_python(
        1.0,
        mode,
        gp=gp,
        seconds=sec,
        pace_seconds=pace_seconds,
    )
    min_val: Optional[float]
    if mode == "totals":
        min_val = round(minutes, 1)
    else:
        min_val = round(minutes / gp, 1) if gp else None

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
        "efg_pct": efg_pct_line(fgm=fgm, fga=fga, fg3m=fg3m),
        "ts_pct": ts_pct_line(pts=r.pts, fga=fga, fta=fta),
    }
    return LeaderRow(
        rank=0, slug=r.slug, name=r.display_name or "Player", gp=gp, values=values
    )

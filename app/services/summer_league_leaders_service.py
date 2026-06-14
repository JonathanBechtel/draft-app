"""Read-side service for the Summer League leaders page.

A comprehensive, sortable leaderboard across five display modes:

* ``totals`` / ``per_game`` / ``per_36`` / ``per_100`` — counting stats scaled
  per the mode (per-100 uses NBA-supplied on-court ``pace``).
* ``advanced`` — rate metrics (TS%, eFG%, USG%, PIE, ratings, REB%/AST%)
  computed from box totals (TS/eFG) or minute-weighted (USG/PIE/ratings/%).

Every visible column is sortable. Composite metrics (GameScore, an SL composite)
are scaffolded as placeholder columns pending a methodology decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeaguePlayerGameLog,
)

DEFAULT_MIN_GAMES = 2
DEFAULT_MIN_MINUTES = 60
PAGE_SIZE = 25
_MINUTES_PER_GAME = 48.0

MODES = ("totals", "per_game", "per_36", "per_100", "advanced")
MODE_LABELS = {
    "totals": "Totals",
    "per_game": "Per Game",
    "per_36": "Per 36",
    "per_100": "Per 100",
    "advanced": "Advanced",
}

# Counting stats that scale with the per-game / per-36 / per-100 transforms.
_COUNTING = ("pts", "reb", "ast", "stl", "blk", "tov")

# Column metadata: key -> (label, kind). kind drives template formatting and the
# in-service computation. "count" scales by mode; "pct"/"rating" do not;
# "placeholder" has no data yet.
COLUMN_LABELS: dict[str, str] = {
    "min": "MIN",
    "pts": "PTS",
    "reb": "REB",
    "ast": "AST",
    "stl": "STL",
    "blk": "BLK",
    "tov": "TOV",
    "fg_pct": "FG%",
    "fg3_pct": "3P%",
    "ft_pct": "FT%",
    "ts_pct": "TS%",
    "efg_pct": "eFG%",
    "usg_pct": "USG%",
    "pie": "PIE",
    "off_rating": "ORtg",
    "def_rating": "DRtg",
    "net_rating": "NetRtg",
    "reb_pct": "REB%",
    "ast_pct": "AST%",
    "gamescore": "GmSc",
    "sl_score": "SL Score",
}
_PCT_COLS = {
    "fg_pct",
    "fg3_pct",
    "ft_pct",
    "ts_pct",
    "efg_pct",
    "usg_pct",
    "reb_pct",
    "ast_pct",
}
_PLACEHOLDER_COLS = {"gamescore", "sl_score"}

_COUNTING_COLUMNS = [
    "min",
    *_COUNTING,
    "fg_pct",
    "fg3_pct",
    "ft_pct",
    "ts_pct",
]
_ADVANCED_COLUMNS = [
    "min",
    "ts_pct",
    "efg_pct",
    "usg_pct",
    "pie",
    "off_rating",
    "def_rating",
    "net_rating",
    "reb_pct",
    "ast_pct",
    "gamescore",
    "sl_score",
]
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


async def get_leaders(
    db: AsyncSession,
    *,
    mode: str = "per_game",
    year: Optional[int] = None,
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
    columns_keys = MODE_COLUMNS[mode]
    if sort not in columns_keys or sort in _PLACEHOLDER_COLS:
        sort = "pie" if mode == "advanced" else "pts"

    rows = await _aggregate(db, year=year, min_games=min_games, min_minutes=min_minutes)
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
        LeaderColumn(
            key=k,
            label=COLUMN_LABELS[k],
            sortable=k not in _PLACEHOLDER_COLS,
            placeholder=k in _PLACEHOLDER_COLS,
        )
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
    )


async def _aggregate(
    db: AsyncSession, *, year: Optional[int], min_games: int, min_minutes: int
) -> list[Any]:
    """Aggregate per-player box totals (+ minute-weighted advanced inputs)."""
    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition
    sec: Any = pgl.minutes_seconds  # treat as a column expression for arithmetic

    conds: list[Any] = [
        pgl.player_id.isnot(None),  # type: ignore[union-attr]
        sec > 0,
    ]
    if year is not None:
        conds.append(comp.year == year)  # type: ignore[arg-type]

    def wsum(col: Any) -> Any:
        return func.sum(col * sec)

    stmt = (
        select(
            pgl.player_id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            func.count().label("gp"),
            func.sum(sec).label("sec"),
            func.sum(pgl.pace * sec).label("pace_sec"),
            func.sum(pgl.pts).label("pts"),
            func.sum(pgl.reb).label("reb"),
            func.sum(pgl.ast).label("ast"),
            func.sum(pgl.stl).label("stl"),
            func.sum(pgl.blk).label("blk"),
            func.sum(pgl.tov).label("tov"),
            func.sum(pgl.fgm).label("fgm"),
            func.sum(pgl.fga).label("fga"),
            func.sum(pgl.fg3m).label("fg3m"),
            func.sum(pgl.fg3a).label("fg3a"),
            func.sum(pgl.ftm).label("ftm"),
            func.sum(pgl.fta).label("fta"),
            wsum(pgl.usg_pct).label("w_usg"),
            wsum(pgl.pie).label("w_pie"),
            wsum(pgl.off_rating).label("w_off"),
            wsum(pgl.def_rating).label("w_def"),
            wsum(pgl.net_rating).label("w_net"),
            wsum(pgl.reb_pct).label("w_reb"),
            wsum(pgl.ast_pct).label("w_ast"),
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
    minutes = (r.sec or 0) / 60.0
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
    else:  # totals / advanced
        factor = 1.0
        min_val = (
            round(minutes, 1)
            if mode == "totals"
            else (round(minutes / gp, 1) if gp else None)
        )

    def scaled(total: Optional[float]) -> Optional[float]:
        if factor is None or total is None:
            return None
        v = total * factor
        return round(v) if mode == "totals" else round(v, 1)

    fga, fta = float(r.fga or 0), float(r.fta or 0)
    values: dict[str, Optional[float]] = {
        "min": min_val,
        **{c: scaled(float(getattr(r, c) or 0)) for c in _COUNTING},
        "fg_pct": _pct(_safe_div(float(r.fgm or 0), fga)),
        "fg3_pct": _pct(_safe_div(float(r.fg3m or 0), float(r.fg3a or 0))),
        "ft_pct": _pct(_safe_div(float(r.ftm or 0), fta)),
        "ts_pct": _pct(_safe_div(float(r.pts or 0), 2.0 * (fga + 0.44 * fta))),
        "efg_pct": _pct(_safe_div(float(r.fgm or 0) + 0.5 * float(r.fg3m or 0), fga)),
        # Minute-weighted on-court rates.
        "usg_pct": _round1(_safe_div(float(r.w_usg or 0), float(r.sec or 0))),
        "pie": _round1(_safe_div(float(r.w_pie or 0) * 100.0, float(r.sec or 0))),
        "off_rating": _round1(_safe_div(float(r.w_off or 0), float(r.sec or 0))),
        "def_rating": _round1(_safe_div(float(r.w_def or 0), float(r.sec or 0))),
        "net_rating": _round1(_safe_div(float(r.w_net or 0), float(r.sec or 0))),
        "reb_pct": _round1(_safe_div(float(r.w_reb or 0), float(r.sec or 0))),
        "ast_pct": _round1(_safe_div(float(r.w_ast or 0), float(r.sec or 0))),
        # Composite metrics pending methodology.
        "gamescore": None,
        "sl_score": None,
    }
    return LeaderRow(
        rank=0, slug=r.slug, name=r.display_name or "Player", gp=gp, values=values
    )


def _round1(v: Optional[float]) -> Optional[float]:
    return round(v, 1) if v is not None else None

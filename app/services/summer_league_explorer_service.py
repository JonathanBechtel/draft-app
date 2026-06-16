"""Read-side service for the Summer League Explorer (faceted query builder).

`/stats/summer-league/explorer` — a Stathead-style builder that turns URL-encoded
filters into a sortable, paginated table. State lives entirely in the query
string so every view is shareable.

Three subjects are planned (players, teams, games); this module dispatches by
subject and currently implements **players** (Phase 1). Teams and games return an
empty, ``available=False`` result until their phases land, so the UI can show the
subject toggle without breaking.

Players rows aggregate a player's box totals across every game inside the filter
scope (year range, venue, draft class/round), then scale to the selected per-mode
view. Composite metrics that don't sum across competition pools (PER/BPM/etc.) are
intentionally excluded here — only additive box stats and ratio shooting metrics,
which recombine correctly from summed makes/attempts.
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
from app.services.summer_league_games_service import _venue_label

SUBJECTS = ("players", "teams", "games")
DEFAULT_SUBJECT = "players"

DEFAULT_MIN_GAMES = 2
DEFAULT_MIN_MINUTES = 60
PAGE_SIZE = 50
MODES = ("per_game", "per_36", "per_100", "totals")
DEFAULT_MODE = "per_game"

_MINUTES_PER_GAME = 48.0


# --------------------------------------------------------------------------- #
# Column catalog (players)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExplorerColumn:
    """One sortable result column."""

    key: str
    label: str
    numeric: bool = True


# Stat columns shown for the players subject, in display order.
_PLAYER_STAT_COLUMNS: list[ExplorerColumn] = [
    ExplorerColumn("gp", "GP"),
    ExplorerColumn("min", "MIN"),
    ExplorerColumn("pts", "PTS"),
    ExplorerColumn("reb", "REB"),
    ExplorerColumn("ast", "AST"),
    ExplorerColumn("stl", "STL"),
    ExplorerColumn("blk", "BLK"),
    ExplorerColumn("tov", "TOV"),
    ExplorerColumn("fg_pct", "FG%"),
    ExplorerColumn("fg3_pct", "3P%"),
    ExplorerColumn("ft_pct", "FT%"),
    ExplorerColumn("ts_pct", "TS%"),
]
_PLAYER_SORT_KEYS = {c.key for c in _PLAYER_STAT_COLUMNS}
_COUNTING = ("pts", "reb", "ast", "stl", "blk", "tov")


# --------------------------------------------------------------------------- #
# DTOs
# --------------------------------------------------------------------------- #


@dataclass
class ExplorerQuery:
    """Parsed, validated Explorer query state (mirrors the URL params)."""

    subject: str = DEFAULT_SUBJECT
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    venue: Optional[str] = None
    draft_class: Optional[int] = None
    draft_round: Optional[int] = None
    min_games: int = DEFAULT_MIN_GAMES
    min_minutes: int = DEFAULT_MIN_MINUTES
    mode: str = DEFAULT_MODE
    sort: str = "pts"
    direction: str = "desc"
    page: int = 1


@dataclass
class ExplorerRow:
    """One result row: a label cell (with optional link) plus stat values."""

    label: str
    href: Optional[str]
    values: dict[str, Any]


@dataclass
class ExplorerFacets:
    """Choices offered by the query-builder panel."""

    years: list[int] = field(default_factory=list)
    venues: list[tuple[str, str]] = field(default_factory=list)
    draft_classes: list[int] = field(default_factory=list)


@dataclass
class ExplorerResult:
    """A rendered Explorer query: columns, rows, pagination, and facets."""

    subject: str
    available: bool
    columns: list[ExplorerColumn]
    rows: list[ExplorerRow]
    total: int
    page: int
    page_size: int
    has_next: bool
    facets: ExplorerFacets
    query: ExplorerQuery


# --------------------------------------------------------------------------- #
# Query parsing
# --------------------------------------------------------------------------- #


def _to_int(value: Optional[str]) -> Optional[int]:
    """Best-effort int parse; ``None`` for blank/invalid so filters degrade off."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_query(params: dict[str, str]) -> ExplorerQuery:
    """Build a validated :class:`ExplorerQuery` from raw query-string params."""
    subject = params.get("subject", DEFAULT_SUBJECT)
    if subject not in SUBJECTS:
        subject = DEFAULT_SUBJECT

    mode = params.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE

    sort = params.get("sort", "pts")
    if sort not in _PLAYER_SORT_KEYS:
        sort = "pts"

    direction = params.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"

    venue = params.get("venue") or None

    min_games = _to_int(params.get("min_gp"))
    min_minutes = _to_int(params.get("min_min"))
    page = _to_int(params.get("page")) or 1

    return ExplorerQuery(
        subject=subject,
        year_min=_to_int(params.get("year_min")),
        year_max=_to_int(params.get("year_max")),
        venue=venue,
        draft_class=_to_int(params.get("draft_class")),
        draft_round=_to_int(params.get("draft_round")),
        min_games=min_games if min_games is not None else DEFAULT_MIN_GAMES,
        min_minutes=min_minutes if min_minutes is not None else DEFAULT_MIN_MINUTES,
        mode=mode,
        sort=sort,
        direction=direction,
        page=max(1, page),
    )


# --------------------------------------------------------------------------- #
# Facets
# --------------------------------------------------------------------------- #


async def get_facets(db: AsyncSession) -> ExplorerFacets:
    """Return the year/venue/draft-class choices for the builder dropdowns."""
    years = [
        int(y)
        for (y,) in (
            await db.execute(
                select(SummerLeagueCompetition.year)  # type: ignore[call-overload]
                .distinct()
                .order_by(SummerLeagueCompetition.year.desc())  # type: ignore[attr-defined]
            )
        ).all()
    ]
    venue_slugs = [
        v
        for (v,) in (
            await db.execute(
                select(SummerLeagueCompetition.venue_slug)  # type: ignore[call-overload]
                .distinct()
                .order_by(SummerLeagueCompetition.venue_slug)  # type: ignore[attr-defined]
            )
        ).all()
    ]
    draft_classes = [
        int(y)
        for (y,) in (
            await db.execute(
                select(PlayerMaster.draft_year)  # type: ignore[call-overload]
                .where(PlayerMaster.draft_year.isnot(None))  # type: ignore[union-attr]
                .distinct()
                .order_by(PlayerMaster.draft_year.desc())  # type: ignore[union-attr]
            )
        ).all()
    ]
    return ExplorerFacets(
        years=years,
        venues=[(v, _venue_label(v)) for v in venue_slugs],
        draft_classes=draft_classes,
    )


# --------------------------------------------------------------------------- #
# Players subject
# --------------------------------------------------------------------------- #


def _safe_div(num: float, den: float) -> Optional[float]:
    return num / den if den else None


def _pct(fraction: Optional[float]) -> Optional[float]:
    return round(100.0 * fraction, 1) if fraction is not None else None


def _compute_player_values(r: Any, mode: str) -> dict[str, Any]:
    """Scale one player's summed box totals into the selected per-mode view."""
    gp = int(r.gp)
    sec = float(r.sec or 0)
    minutes = sec / 60.0
    poss = (r.pace_sec or 0) / (60.0 * _MINUTES_PER_GAME)

    if mode == "per_game":
        factor: Optional[float] = _safe_div(1.0, gp)
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

    def scaled(total: float) -> Optional[float]:
        if factor is None:
            return None
        v = total * factor
        return round(v) if mode == "totals" else round(v, 1)

    fga, fta = float(r.fga or 0), float(r.fta or 0)
    return {
        "gp": gp,
        "min": min_val,
        **{c: scaled(float(getattr(r, c) or 0)) for c in _COUNTING},
        "fg_pct": _pct(_safe_div(float(r.fgm or 0), fga)),
        "fg3_pct": _pct(_safe_div(float(r.fg3m or 0), float(r.fg3a or 0))),
        "ft_pct": _pct(_safe_div(float(r.ftm or 0), fta)),
        "ts_pct": _pct(_safe_div(float(r.pts or 0), 2.0 * (fga + 0.44 * fta))),
    }


async def _query_players(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """Aggregate, sort, and paginate the players subject."""
    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition
    pm = PlayerMaster
    sec: Any = pgl.minutes_seconds

    conds: list[Any] = [pgl.player_id.isnot(None), sec > 0]  # type: ignore[union-attr]
    if q.year_min is not None:
        conds.append(comp.year >= q.year_min)  # type: ignore[arg-type]
    if q.year_max is not None:
        conds.append(comp.year <= q.year_max)  # type: ignore[arg-type]
    if q.venue:
        conds.append(comp.venue_slug == q.venue)
    if q.draft_class is not None:
        conds.append(pm.draft_year == q.draft_class)  # type: ignore[arg-type]
    if q.draft_round is not None:
        conds.append(pm.draft_round == q.draft_round)  # type: ignore[arg-type]

    stmt = (
        select(
            pm.slug,
            pm.display_name,
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
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(pm, pm.id == pgl.player_id)
        .where(*conds)
        .group_by(pgl.player_id, pm.slug, pm.display_name)
        .having(func.count() >= q.min_games)
        .having(func.sum(sec) >= q.min_minutes * 60)
    )
    raw = list((await db.execute(stmt)).all())

    rows = [
        ExplorerRow(
            label=r.display_name or "Player",
            href=f"/players/{r.slug}" if r.slug else None,
            values=_compute_player_values(r, q.mode),
        )
        for r in raw
    ]

    # Sort in Python on the mode-scaled value (nulls sort last regardless of dir).
    reverse = q.direction == "desc"
    rows.sort(
        key=lambda row: (
            row.values.get(q.sort) is None,
            -(row.values.get(q.sort) or 0)
            if reverse
            else (row.values.get(q.sort) or 0),
        )
    )

    total = len(rows)
    start = (q.page - 1) * PAGE_SIZE
    page_rows = rows[start : start + PAGE_SIZE]

    return ExplorerResult(
        subject="players",
        available=True,
        columns=_PLAYER_STAT_COLUMNS,
        rows=page_rows,
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=start + PAGE_SIZE < total,
        facets=ExplorerFacets(),  # filled by the caller
        query=q,
    )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


async def run_explorer_query(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """Run the Explorer query for the requested subject and attach facets."""
    facets = await get_facets(db)

    if q.subject == "players":
        result = await _query_players(db, q)
        result.facets = facets
        return result

    # Teams/games land in later phases; return an empty, unavailable result so
    # the subject toggle renders without error.
    return ExplorerResult(
        subject=q.subject,
        available=False,
        columns=[],
        rows=[],
        total=0,
        page=1,
        page_size=PAGE_SIZE,
        has_next=False,
        facets=facets,
        query=q,
    )

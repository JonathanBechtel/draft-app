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

from sqlalchemy import func, literal, or_, select
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
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
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
    ExplorerColumn("oreb", "OREB"),
    ExplorerColumn("dreb", "DREB"),
    ExplorerColumn("pf", "PF"),
    ExplorerColumn("plus_minus", "+/-"),
    ExplorerColumn("efg_pct", "eFG%"),
    ExplorerColumn("fgm", "FGM"),
    ExplorerColumn("fga", "FGA"),
    ExplorerColumn("fg3m", "3PM"),
    ExplorerColumn("fg3a", "3PA"),
    ExplorerColumn("ftm", "FTM"),
    ExplorerColumn("fta", "FTA"),
    ExplorerColumn("fg_pct", "FG%"),
    ExplorerColumn("fg3_pct", "3P%"),
    ExplorerColumn("ft_pct", "FT%"),
    ExplorerColumn("ts_pct", "TS%"),
]

# Stat columns for the teams subject (one row per team-season). W-L and points
# come from game scores; pace/ratings are team-box-log averages where present.
_TEAM_STAT_COLUMNS: list[ExplorerColumn] = [
    ExplorerColumn("gp", "GP"),
    ExplorerColumn("w", "W"),
    ExplorerColumn("l", "L"),
    ExplorerColumn("ppg", "PPG"),
    ExplorerColumn("opp_ppg", "OPP"),
    ExplorerColumn("diff", "DIFF"),
    ExplorerColumn("pace", "PACE"),
    ExplorerColumn("ortg", "ORtg"),
    ExplorerColumn("drtg", "DRtg"),
]

# Stat columns for the games subject (one row per game). The label carries date
# + matchup + score; these are the sortable numeric dimensions.
_GAME_STAT_COLUMNS: list[ExplorerColumn] = [
    ExplorerColumn("total", "Total"),
    ExplorerColumn("margin", "Margin"),
]

_COLUMNS_BY_SUBJECT: dict[str, list[ExplorerColumn]] = {
    "players": _PLAYER_STAT_COLUMNS,
    "teams": _TEAM_STAT_COLUMNS,
    "games": _GAME_STAT_COLUMNS,
}
_SORT_KEYS_BY_SUBJECT: dict[str, set[str]] = {
    s: {c.key for c in cols} for s, cols in _COLUMNS_BY_SUBJECT.items()
}
_DEFAULT_SORT_BY_SUBJECT: dict[str, str] = {
    "players": "pts",
    "teams": "diff",
    "games": "total",
}
_COUNTING = (
    "pts",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "oreb",
    "dreb",
    "pf",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
)


# --------------------------------------------------------------------------- #
# DTOs
# --------------------------------------------------------------------------- #


@dataclass
class ExplorerQuery:
    """Parsed, validated Explorer query state (mirrors the URL params)."""

    subject: str = DEFAULT_SUBJECT
    grain: str = "career"  # "career" | "per_competition" | "per_game"
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    venue: Optional[str] = None
    draft_class: Optional[int] = None
    draft_round: Optional[int] = None
    draft_pick_min: Optional[int] = None
    draft_pick_max: Optional[int] = None
    position: Optional[str] = None
    country: Optional[str] = None
    team_slug: Optional[str] = None
    round_type: Optional[str] = None
    undrafted: bool = False
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
    positions: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    round_types: list[str] = field(default_factory=list)


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

    grain_raw = params.get("grain", "career")
    grain = (
        grain_raw
        if grain_raw in ("career", "per_competition", "per_game")
        else "career"
    )

    mode = params.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE

    # Sort keys are subject-specific; fall back to the subject's default.
    default_sort = _DEFAULT_SORT_BY_SUBJECT.get(subject, "pts")
    sort = params.get("sort", default_sort)
    if sort not in _SORT_KEYS_BY_SUBJECT.get(subject, set()):
        sort = default_sort

    direction = params.get("dir", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"

    venue = params.get("venue") or None
    position = params.get("position") or None
    country = params.get("country") or None
    team_slug = params.get("team_slug") or None
    round_type = params.get("round_type") or None
    undrafted = params.get("undrafted") == "1"

    min_games = _to_int(params.get("min_gp"))
    min_minutes = _to_int(params.get("min_min"))
    page = _to_int(params.get("page")) or 1

    return ExplorerQuery(
        subject=subject,
        grain=grain,
        year_min=_to_int(params.get("year_min")),
        year_max=_to_int(params.get("year_max")),
        venue=venue,
        draft_class=_to_int(params.get("draft_class")),
        draft_round=_to_int(params.get("draft_round")),
        draft_pick_min=_to_int(params.get("draft_pick_min")),
        draft_pick_max=_to_int(params.get("draft_pick_max")),
        position=position,
        country=country,
        team_slug=team_slug,
        round_type=round_type,
        undrafted=undrafted,
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
    positions = [
        str(p)
        for (p,) in (
            await db.execute(
                select(PlayerMaster.position)  # type: ignore[call-overload]
                .where(PlayerMaster.position.isnot(None))  # type: ignore[union-attr]
                .distinct()
                .order_by(PlayerMaster.position)  # type: ignore[union-attr]
            )
        ).all()
    ]
    countries = [
        str(c)
        for (c,) in (
            await db.execute(
                select(PlayerMaster.birth_country)  # type: ignore[call-overload]
                .where(PlayerMaster.birth_country.isnot(None))  # type: ignore[union-attr]
                .distinct()
                .order_by(PlayerMaster.birth_country)  # type: ignore[union-attr]
            )
        ).all()
    ]
    teams = [
        str(s)
        for (s,) in (
            await db.execute(
                select(SummerLeagueTeamEntry.team_slug)  # type: ignore[call-overload]
                .distinct()
                .order_by(SummerLeagueTeamEntry.team_slug)  # type: ignore[union-attr]
            )
        ).all()
    ]
    round_types = [
        str(rt)
        for (rt,) in (
            await db.execute(
                select(SummerLeagueGame.round_label)  # type: ignore[call-overload]
                .where(SummerLeagueGame.round_label.isnot(None))  # type: ignore[union-attr]
                .distinct()
                .order_by(SummerLeagueGame.round_label)  # type: ignore[union-attr]
            )
        ).all()
    ]
    return ExplorerFacets(
        years=years,
        venues=[(v, _venue_label(v)) for v in venue_slugs],
        draft_classes=draft_classes,
        positions=positions,
        countries=countries,
        teams=teams,
        round_types=round_types,
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

    if mode == "per_game":
        plus_minus_val: Optional[float] = (
            round(float(r.plus_minus or 0) / gp, 1) if gp else None
        )
    elif mode == "totals":
        plus_minus_val = round(float(r.plus_minus or 0))
    else:
        plus_minus_val = None

    return {
        "gp": gp,
        "min": min_val,
        **{c: scaled(float(getattr(r, c) or 0)) for c in _COUNTING},
        "plus_minus": plus_minus_val,
        "efg_pct": _pct(_safe_div(float(r.fgm or 0) + 0.5 * float(r.fg3m or 0), fga)),
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
    if q.undrafted:
        conds.append(pm.draft_year.is_(None))  # type: ignore[union-attr]
    else:
        if q.draft_class is not None:
            conds.append(pm.draft_year == q.draft_class)  # type: ignore[arg-type]
        if q.draft_round is not None:
            conds.append(pm.draft_round == q.draft_round)  # type: ignore[arg-type]
        if q.draft_pick_min is not None:
            conds.append(pm.draft_pick >= q.draft_pick_min)  # type: ignore[operator, arg-type]
        if q.draft_pick_max is not None:
            conds.append(pm.draft_pick <= q.draft_pick_max)  # type: ignore[operator, arg-type]
    if q.position is not None:
        conds.append(pm.position == q.position)  # type: ignore[arg-type]
    if q.country is not None:
        conds.append(pm.birth_country == q.country)  # type: ignore[arg-type]

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
            func.sum(pgl.oreb).label("oreb"),
            func.sum(pgl.dreb).label("dreb"),
            func.sum(pgl.pf).label("pf"),
            func.sum(pgl.plus_minus).label("plus_minus"),
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(pm, pm.id == pgl.player_id)
        .where(*conds)
        .group_by(pgl.player_id, pm.slug, pm.display_name)
        .having(func.count() >= q.min_games)
        .having(func.sum(sec) >= q.min_minutes * 60)
    )
    if q.team_slug is not None:
        te = SummerLeagueTeamEntry
        stmt = stmt.join(te, pgl.team_entry_id == te.id).where(  # type: ignore[arg-type]
            te.team_slug == q.team_slug
        )
    if q.round_type is not None:
        g = SummerLeagueGame
        stmt = stmt.join(g, pgl.game_id == g.id).where(g.round_label == q.round_type)
    raw = list((await db.execute(stmt)).all())

    rows = [
        ExplorerRow(
            label=r.display_name or "Player",
            href=f"/players/{r.slug}" if r.slug else None,
            values=_compute_player_values(r, q.mode),
        )
        for r in raw
    ]

    return _paginate("players", _PLAYER_STAT_COLUMNS, rows, q)


async def _query_players_per_competition(
    db: AsyncSession, q: ExplorerQuery
) -> ExplorerResult:
    """One row per (player, competition): season box totals from SummerLeaguePlayerSeason."""
    ps = SummerLeaguePlayerSeason
    comp = SummerLeagueCompetition
    pm = PlayerMaster

    # Alias minutes*60 as sec so _compute_player_values works unchanged.
    # pace_sec is 0 — season rows have no weighted pace; per_100 mode yields None.
    conds: list[Any] = [
        ps.gp >= q.min_games,  # type: ignore[operator]
        ps.minutes >= q.min_minutes / 60.0,  # type: ignore[operator]
    ]
    if q.year_min is not None:
        conds.append(ps.year >= q.year_min)  # type: ignore[arg-type]
    if q.year_max is not None:
        conds.append(ps.year <= q.year_max)  # type: ignore[arg-type]
    if q.venue:
        conds.append(ps.venue_slug == q.venue)
    if q.undrafted:
        conds.append(pm.draft_year.is_(None))  # type: ignore[union-attr]
    else:
        if q.draft_class is not None:
            conds.append(pm.draft_year == q.draft_class)  # type: ignore[arg-type]
        if q.draft_round is not None:
            conds.append(pm.draft_round == q.draft_round)  # type: ignore[arg-type]
    if q.position is not None:
        conds.append(pm.position == q.position)  # type: ignore[arg-type]

    stmt = (
        select(
            pm.slug,
            pm.display_name,
            ps.year,
            ps.venue_slug,
            ps.gp.label("gp"),  # type: ignore[attr-defined]
            (ps.minutes * 60).label("sec"),  # type: ignore[attr-defined]
            literal(0).label("pace_sec"),
            ps.pts.label("pts"),  # type: ignore[attr-defined]
            ps.reb.label("reb"),  # type: ignore[attr-defined]
            ps.ast.label("ast"),  # type: ignore[attr-defined]
            ps.stl.label("stl"),  # type: ignore[attr-defined]
            ps.blk.label("blk"),  # type: ignore[attr-defined]
            ps.tov.label("tov"),  # type: ignore[attr-defined]
            ps.fgm.label("fgm"),  # type: ignore[attr-defined]
            ps.fga.label("fga"),  # type: ignore[attr-defined]
            ps.fg3m.label("fg3m"),  # type: ignore[attr-defined]
            ps.fg3a.label("fg3a"),  # type: ignore[attr-defined]
            ps.ftm.label("ftm"),  # type: ignore[attr-defined]
            ps.fta.label("fta"),  # type: ignore[attr-defined]
            ps.oreb.label("oreb"),  # type: ignore[attr-defined]
            ps.dreb.label("dreb"),  # type: ignore[attr-defined]
            ps.pf.label("pf"),  # type: ignore[attr-defined]
            ps.plus_minus.label("plus_minus"),  # type: ignore[attr-defined]
        )  # type: ignore[call-overload, misc]
        .select_from(ps)
        .join(comp, comp.id == ps.competition_id)
        .join(pm, pm.id == ps.player_id)
        .where(*conds)
    )

    raw = list((await db.execute(stmt)).all())

    rows = [
        ExplorerRow(
            label=f"{r.display_name} · {_venue_label(r.venue_slug)} {r.year}",
            href=f"/players/{r.slug}" if r.slug else None,
            values=_compute_player_values(r, q.mode),
        )
        for r in raw
    ]

    return _paginate("players", _PLAYER_STAT_COLUMNS, rows, q)


async def _query_players_per_game(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """One row per player game log (no aggregation)."""
    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition
    pm = PlayerMaster
    game = SummerLeagueGame

    conds: list[Any] = [pgl.player_id.isnot(None), pgl.minutes_seconds > 0]  # type: ignore[union-attr, operator]
    if q.year_min is not None:
        conds.append(comp.year >= q.year_min)  # type: ignore[arg-type]
    if q.year_max is not None:
        conds.append(comp.year <= q.year_max)  # type: ignore[arg-type]
    if q.venue:
        conds.append(comp.venue_slug == q.venue)
    if q.undrafted:
        conds.append(pm.draft_year.is_(None))  # type: ignore[union-attr]
    else:
        if q.draft_class is not None:
            conds.append(pm.draft_year == q.draft_class)  # type: ignore[arg-type]
        if q.draft_round is not None:
            conds.append(pm.draft_round == q.draft_round)  # type: ignore[arg-type]
    if q.position is not None:
        conds.append(pm.position == q.position)  # type: ignore[arg-type]

    stmt = (
        select(
            pm.slug,
            pm.display_name,
            game.game_date,
            game.id.label("game_id"),  # type: ignore[attr-defined, union-attr]
            comp.year,
            comp.venue_slug,
            pgl.minutes_seconds.label("sec"),  # type: ignore[union-attr]
            pgl.pts.label("pts"),  # type: ignore[union-attr]
            pgl.reb.label("reb"),  # type: ignore[union-attr]
            pgl.ast.label("ast"),  # type: ignore[union-attr]
            pgl.stl.label("stl"),  # type: ignore[union-attr]
            pgl.blk.label("blk"),  # type: ignore[union-attr]
            pgl.tov.label("tov"),  # type: ignore[union-attr]
            pgl.fgm.label("fgm"),  # type: ignore[union-attr]
            pgl.fga.label("fga"),  # type: ignore[union-attr]
            pgl.fg3m.label("fg3m"),  # type: ignore[union-attr]
            pgl.fg3a.label("fg3a"),  # type: ignore[union-attr]
            pgl.ftm.label("ftm"),  # type: ignore[union-attr]
            pgl.fta.label("fta"),  # type: ignore[union-attr]
            pgl.oreb.label("oreb"),  # type: ignore[union-attr]
            pgl.dreb.label("dreb"),  # type: ignore[union-attr]
            pgl.pf.label("pf"),  # type: ignore[union-attr]
            pgl.plus_minus.label("plus_minus"),  # type: ignore[union-attr]
            # pace_sec: 0 for single-game rows (per_100 mode will show None)
            literal(0).label("pace_sec"),
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(pm, pm.id == pgl.player_id)
        .join(game, game.id == pgl.game_id)
        .where(*conds)
    )

    raw = list((await db.execute(stmt)).all())

    rows = []
    for r in raw:
        date_str = r.game_date.isoformat() if r.game_date else "—"
        # Build a namespace with gp=1 so _compute_player_values treats each row as one game.
        row_ns = _SingleGameRow(r)
        rows.append(
            ExplorerRow(
                label=f"{r.display_name} · {date_str}",
                href=f"/stats/summer-league/{r.year}/games/{r.game_id}",
                values=_compute_player_values(row_ns, q.mode),
            )
        )

    return _paginate("players", _PLAYER_STAT_COLUMNS, rows, q)


class _SingleGameRow:
    """Thin adapter that exposes a game-log row as gp=1 for _compute_player_values."""

    __slots__ = (
        "gp",
        "sec",
        "pace_sec",
        "pts",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "fgm",
        "fga",
        "fg3m",
        "fg3a",
        "ftm",
        "fta",
        "oreb",
        "dreb",
        "pf",
        "plus_minus",
    )

    def __init__(self, row: Any) -> None:
        self.gp = 1
        self.sec = row.sec
        self.pace_sec = 0
        self.pts = row.pts
        self.reb = row.reb
        self.ast = row.ast
        self.stl = row.stl
        self.blk = row.blk
        self.tov = row.tov
        self.fgm = row.fgm
        self.fga = row.fga
        self.fg3m = row.fg3m
        self.fg3a = row.fg3a
        self.ftm = row.ftm
        self.fta = row.fta
        self.oreb = row.oreb
        self.dreb = row.dreb
        self.pf = row.pf
        self.plus_minus = row.plus_minus


# --------------------------------------------------------------------------- #
# Teams subject
# --------------------------------------------------------------------------- #


async def _query_teams(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """One row per team-season: record + scoring from games, ratings from box logs."""
    te = SummerLeagueTeamEntry
    comp = SummerLeagueCompetition

    conds: list[Any] = []
    if q.year_min is not None:
        conds.append(comp.year >= q.year_min)  # type: ignore[arg-type]
    if q.year_max is not None:
        conds.append(comp.year <= q.year_max)  # type: ignore[arg-type]
    if q.venue:
        conds.append(comp.venue_slug == q.venue)

    entry_rows = (
        await db.execute(
            select(
                te.id,
                te.team_slug,
                te.raw_team_name,
                te.raw_team_abbreviation,
                comp.year,
                comp.venue_slug,
            )  # type: ignore[call-overload, misc]
            .select_from(te)
            .join(comp, comp.id == te.competition_id)
            .where(*conds)
        )
    ).all()
    if not entry_rows:
        return _empty_result("teams", q)

    entry_ids = [r.id for r in entry_rows]
    entry_set = set(entry_ids)

    # Win/loss + points-for/against from game scores (complete; plus_minus is not).
    game = SummerLeagueGame
    game_scope_conds: list[Any] = [
        or_(
            game.home_team_entry_id.in_(entry_ids),  # type: ignore[union-attr]
            game.away_team_entry_id.in_(entry_ids),  # type: ignore[union-attr]
        )
    ]
    if q.round_type is not None:
        game_scope_conds.append(game.round_label == q.round_type)
    games = (
        await db.execute(
            select(
                game.home_team_entry_id,
                game.away_team_entry_id,
                game.home_score,
                game.away_score,
            ).where(*game_scope_conds)  # type: ignore[call-overload]
        )
    ).all()

    rec: dict[int, list[int]] = {e: [0, 0, 0, 0, 0] for e in entry_ids}  # gp,w,l,pf,pa
    for g in games:
        if g.home_score is None or g.away_score is None:
            continue
        # Both teams are typically in scope, so credit each from its own side.
        for eid, mine, opp in (
            (g.home_team_entry_id, g.home_score, g.away_score),
            (g.away_team_entry_id, g.away_score, g.home_score),
        ):
            if eid not in entry_set:
                continue
            s = rec[eid]
            s[0] += 1
            s[1 if mine > opp else 2] += 1
            s[3] += mine
            s[4] += opp

    # Pace / efficiency from team box logs (averaged where present).
    tgl = SummerLeagueTeamGameLog
    rating_rows = (
        await db.execute(
            select(
                tgl.team_entry_id,
                func.avg(tgl.pace).label("pace"),
                func.avg(tgl.off_rating).label("ortg"),
                func.avg(tgl.def_rating).label("drtg"),
            )  # type: ignore[call-overload, misc]
            .where(tgl.team_entry_id.in_(entry_ids))  # type: ignore[attr-defined]
            .group_by(tgl.team_entry_id)
        )
    ).all()
    ratings = {r.team_entry_id: r for r in rating_rows}

    def _r1(v: Any) -> Optional[float]:
        return round(float(v), 1) if v is not None else None

    rows: list[ExplorerRow] = []
    for r in entry_rows:
        gp, w, lo, pf, pa = rec[r.id]
        if gp == 0:
            continue
        rt = ratings.get(r.id)
        name = r.raw_team_name or r.raw_team_abbreviation or "Team"
        rows.append(
            ExplorerRow(
                label=f"{name} · {_venue_label(r.venue_slug)} {r.year}",
                href=f"/stats/summer-league/{r.year}/{r.venue_slug}/{r.team_slug}",
                values={
                    "gp": gp,
                    "w": w,
                    "l": lo,
                    "ppg": round(pf / gp, 1),
                    "opp_ppg": round(pa / gp, 1),
                    "diff": round((pf - pa) / gp, 1),
                    "pace": _r1(rt.pace) if rt else None,
                    "ortg": _r1(rt.ortg) if rt else None,
                    "drtg": _r1(rt.drtg) if rt else None,
                },
            )
        )

    return _paginate("teams", _TEAM_STAT_COLUMNS, rows, q)


# --------------------------------------------------------------------------- #
# Games subject
# --------------------------------------------------------------------------- #


async def _query_games(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """One row per game: matchup/score in the label, total + margin sortable."""
    game = SummerLeagueGame
    comp = SummerLeagueCompetition
    home = aliased(SummerLeagueTeamEntry)
    away = aliased(SummerLeagueTeamEntry)

    conds: list[Any] = [game.home_score.isnot(None), game.away_score.isnot(None)]  # type: ignore[union-attr]
    if q.year_min is not None:
        conds.append(comp.year >= q.year_min)  # type: ignore[arg-type]
    if q.year_max is not None:
        conds.append(comp.year <= q.year_max)  # type: ignore[arg-type]
    if q.venue:
        conds.append(comp.venue_slug == q.venue)
    if q.round_type is not None:
        conds.append(game.round_label == q.round_type)

    game_rows = (
        await db.execute(
            select(
                game.id,
                game.game_date,
                game.home_score,
                game.away_score,
                comp.year,
                comp.venue_slug,
                home.raw_team_abbreviation.label("home_abbr"),  # type: ignore[union-attr]
                home.raw_team_name.label("home_name"),  # type: ignore[attr-defined]
                away.raw_team_abbreviation.label("away_abbr"),  # type: ignore[union-attr]
                away.raw_team_name.label("away_name"),  # type: ignore[attr-defined]
            )  # type: ignore[call-overload, misc]
            .select_from(game)
            .join(comp, comp.id == game.competition_id)
            .join(home, home.id == game.home_team_entry_id, isouter=True)
            .join(away, away.id == game.away_team_entry_id, isouter=True)
            .where(*conds)
        )
    ).all()

    rows: list[ExplorerRow] = []
    for r in game_rows:
        home_label = r.home_abbr or r.home_name or "Home"
        away_label = r.away_abbr or r.away_name or "Away"
        date_str = r.game_date.isoformat() if r.game_date else "—"
        rows.append(
            ExplorerRow(
                label=(
                    f"{date_str} · {away_label} {r.away_score} "
                    f"@ {home_label} {r.home_score}"
                ),
                href=f"/stats/summer-league/{r.year}/games/{r.id}",
                values={
                    "total": r.home_score + r.away_score,
                    "margin": abs(r.home_score - r.away_score),
                },
            )
        )

    return _paginate("games", _GAME_STAT_COLUMNS, rows, q)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _paginate(
    subject: str,
    columns: list[ExplorerColumn],
    rows: list[ExplorerRow],
    q: ExplorerQuery,
) -> ExplorerResult:
    """Sort rows by the query's column (nulls last), then slice to the page."""
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
    return ExplorerResult(
        subject=subject,
        available=True,
        columns=columns,
        rows=rows[start : start + PAGE_SIZE],
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=start + PAGE_SIZE < total,
        facets=ExplorerFacets(),  # filled by the caller
        query=q,
    )


def _empty_result(subject: str, q: ExplorerQuery) -> ExplorerResult:
    """An available result with no rows (e.g. filters matched nothing)."""
    return _paginate(subject, _COLUMNS_BY_SUBJECT.get(subject, []), [], q)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


async def run_explorer_query(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """Run the Explorer query for the requested subject and attach facets."""
    facets = await get_facets(db)

    if q.subject == "players":
        if q.grain == "per_competition":
            result = await _query_players_per_competition(db, q)
        elif q.grain == "per_game":
            result = await _query_players_per_game(db, q)
        else:  # career (default)
            result = await _query_players(db, q)
        result.facets = facets
        return result

    if q.subject == "teams":
        result = await _query_teams(db, q)
        result.facets = facets
        return result

    result = await _query_games(db, q)
    result.facets = facets
    return result

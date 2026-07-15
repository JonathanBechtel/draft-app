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

## Pagination strategy (Phase 3)

All subjects/grains paginate entirely in SQL using ORDER BY + LIMIT + OFFSET.
The total row count is obtained via a wrapping subquery:

    SELECT count(*) FROM (<unsliced statement>) AS _count_sq

This avoids fetching all rows into Python (previously done by `_paginate()`).

Sort-column mapping for the players career/per_competition grains:
- Counting stats (pts, reb, ast, …) → sort on the raw SUM aggregate; this is
  monotonically equivalent to sorting on any per-mode rate (per-game, per-36,
  per-100) because all rows are divided by the same denominator within a grain.
- Percentage stats (efg_pct, fg_pct, fg3_pct, ft_pct, ts_pct, min) → a SQL
  expression is computed inline and used as the ORDER BY clause.
- gp, plus_minus → direct aggregate labels.

For the per_game grain the sort column maps directly to a raw column label.

For teams and games, derived numeric values (diff, total, margin) are expressed
as SQL arithmetic inside a CTE/subquery so they can be sorted in SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from sqlalchemy import and_, case, func, literal, nulls_last, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.position_taxonomy import (
    FINE_SCOPE_PRESET,
    PARENT_SCOPE_PRESET,
    get_parents_for_fine,
)
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.constants import MINUTES_PER_GAME
from app.services.summer_league.metrics import game_score_from_row
from app.services.summer_league_games_service import _venue_label
from app.utils.country import canonical_country, country_variants

SUBJECTS = ("players", "teams", "games")
DEFAULT_SUBJECT = "players"

DEFAULT_MIN_GAMES = 2
DEFAULT_MIN_MINUTES = 60
PAGE_SIZE = 50
MODES = ("per_game", "per_36", "per_100", "totals")
DEFAULT_MODE = "per_game"

# "Nth summer-league appearance" filter (players subject). A player's *appearances*
# are their DISTINCT calendar years in the SL pool, dense-ranked ascending — both
# venues of a single summer share one number (an appearance is a summer, not a
# venue). Options 1..3 isolate that exact appearance; ``APPEARANCE_TOP`` (4) is the
# open-ended "4th or later" bucket, since the multi-return tail is sparse. The rank
# is derived on the fly (no stored column) and always computed over the player's
# full history, so "2nd" means the same season regardless of any year/venue scope.
APPEARANCE_MIN = 1
APPEARANCE_TOP = 4

_MINUTES_PER_GAME = MINUTES_PER_GAME

# Position filter vocabulary, sourced from the canonical taxonomy
# (app/models/position_taxonomy.py) — the same module that derives
# ``positions.code``/``parents`` at ingest time, so filter and data can't
# drift. Canonical position data lives in ``player_status.position_id`` →
# ``positions`` (PlayerMaster.position is unpopulated for the SL pool).
# Codes are lowercase fine tokens, possibly hybrid ("pg", "pg_sg", "pf_c"):
# slot filters match any component of a hybrid code; bucket filters match
# the parent hierarchy.
_POSITION_SLOTS = tuple(t for t in FINE_SCOPE_PRESET if "-" not in t)
_POSITION_BUCKETS = tuple(PARENT_SCOPE_PRESET)
_POSITION_BUCKET_LABELS = {
    "guard": "Guards",
    "wing": "Wings",
    "forward": "Forwards",
    "big": "Bigs",
}
_POSITION_FILTER_VALUES = frozenset(_POSITION_SLOTS) | frozenset(_POSITION_BUCKETS)


def _position_scope_cond(position: str) -> Any:
    """WHERE condition on the ``positions`` table for a slot or bucket value.

    Buckets ("guard", "big", …) match via JSONB containment on
    ``positions.parents``; slots ("pg", "c", …) match any component of the
    (possibly hybrid) ``positions.code``, so ``pg`` includes ``pg_sg``.
    """
    if position in _POSITION_BUCKETS:
        return Position.parents.contains([position])  # type: ignore[union-attr]
    # "_" is the fine-token delimiter written by derive_position_tags.
    return literal(position) == func.any_(func.string_to_array(Position.code, "_"))


def _apply_position_filter(stmt: Any, q: ExplorerQuery) -> Any:
    """Add the position filter to a player statement (no-op when unset).

    Joins ``player_status`` → ``positions`` off PlayerMaster; one status row
    per player, so the join has no row fanout. Joined only when the filter
    is active.
    """
    if q.position is None:
        return stmt
    return (
        stmt.join(PlayerStatus, PlayerStatus.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .join(Position, Position.id == PlayerStatus.position_id)  # type: ignore[arg-type]
        .where(_position_scope_cond(q.position))
    )


def _appearance_rank_subq() -> Any:
    """Subquery mapping ``(player_id, year)`` → the player's SL appearance number.

    A summer-league *appearance* is one distinct calendar year in which the player
    played: two venues in the same summer count once, so the ordinal is a
    ``DENSE_RANK`` over the player's DISTINCT years (ties on year share a number).
    The rank is computed over the player's FULL history in
    ``summer_league_player_seasons`` — deliberately independent of the active
    filters — so "2nd appearance" always resolves to the same season no matter what
    year/venue scope is layered on top.
    """
    ps = SummerLeaguePlayerSeason
    distinct_years = (
        select(ps.player_id, ps.year)  # type: ignore[call-overload]
        .distinct()
        .subquery("_sl_appearance_years")
    )
    appearance_num = func.dense_rank().over(
        partition_by=distinct_years.c.player_id,
        order_by=distinct_years.c.year,
    )
    return select(
        distinct_years.c.player_id.label("player_id"),
        distinct_years.c.year.label("year"),
        appearance_num.label("appearance_num"),
    ).subquery("_sl_appearance_rank")


def _apply_appearance_filter(
    stmt: Any, q: ExplorerQuery, player_id_col: Any, year_col: Any
) -> Any:
    """Restrict a player statement to rows that are the player's Nth SL appearance.

    No-op when the filter is unset. ``APPEARANCE_TOP`` (4) is the open-ended "4th or
    later" bucket; 1–3 isolate that exact appearance. The appearance-rank map is
    joined on ``(player_id, year)`` — one map row per distinct player-year, so the
    join adds no fanout (each input row matches exactly one rank). It is an INNER
    join, so a row whose year has no season row would drop; that should not happen
    because the season table is derived from the same game logs.
    """
    if q.appearance is None:
        return stmt
    ar = _appearance_rank_subq()
    stmt = stmt.join(
        ar,
        and_(ar.c.player_id == player_id_col, ar.c.year == year_col),  # type: ignore[arg-type]
    )
    if q.appearance >= APPEARANCE_TOP:
        return stmt.where(ar.c.appearance_num >= APPEARANCE_TOP)
    return stmt.where(ar.c.appearance_num == q.appearance)


def _max_plausible_draft_class() -> int:
    """Upper bound for draft-class facet/filter values.

    The next legitimately-knowable draft class is one year out; anything beyond
    is a data artifact (the SL pool carries stray 2028–2033 draft years).  Used
    to clamp both the facet dropdown and inbound filter values.
    """
    return date.today().year + 1


# --------------------------------------------------------------------------- #
# Column catalog (players)
# --------------------------------------------------------------------------- #

# Bucket constants — one of three roll-up semantics (see rollup_* functions below).
_BUCKET_RECOMBINABLE = "recombinable"  # exact: recompute from summed box components
_BUCKET_ADDITIVE = "additive"  # exact: sum across pools, null-skip
_BUCKET_RATE_COMPOSITE = (
    "rate_composite"  # approximate: minute-weighted avg across pools
)

# Group constants — broad display category.
_GROUP_BOX = "box"
_GROUP_SHOOTING = "shooting"
_GROUP_ADVANCED = "advanced"


@dataclass(frozen=True)
class ExplorerColumn:
    """One sortable result column with metric taxonomy for roll-up dispatch.

    Fields:
        key:        Unique column identifier (matches query param and result dict key).
        label:      Display label shown in the UI table header.
        group:      Display category: ``"box"`` | ``"shooting"`` | ``"advanced"``.
        bucket:     Roll-up semantic: ``"recombinable"`` | ``"additive"`` |
                    ``"rate_composite"``.  Drives which of the three ``rollup_*``
                    functions should aggregate multi-competition rows of this column.
        sortable:   Whether this column is a valid ORDER BY target in the Explorer.
                    False for catalog-only columns not yet wired into the UI.
        filterable: Whether this column can be used as a min/max stat filter
                    (reserved for future use; currently False for all columns).
        fmt:        Display-format hint: ``"int"`` (no decimals), ``"f1"`` (1 decimal),
                    ``"f2"`` (2 decimals), ``"pct"`` (1 decimal + ``%`` suffix).
        shown:      True when this column appears in the live Explorer column list.
                    False for catalog-classified columns not yet surfaced in the UI
                    (consumed by ticket #405 roll-ups and future phase expansions).
        numeric:    Kept for backward compatibility; True for all numeric columns.
    """

    key: str
    label: str
    group: str = _GROUP_BOX
    bucket: str = _BUCKET_ADDITIVE
    sortable: bool = True
    filterable: bool = False
    fmt: str = "f1"
    shown: bool = True
    numeric: bool = True  # backward compat — always True for player stat columns


# --------------------------------------------------------------------------- #
# Full declarative catalog — every player column with taxonomy.
#
# Ordering within each group mirrors the existing display order so that the
# derived _PLAYER_STAT_COLUMNS / _PLAYER_ADVANCED_COLUMNS lists are identical
# to the lists they replace (preserving all existing caller behaviour).
# --------------------------------------------------------------------------- #
PLAYER_COLUMN_CATALOG: list[ExplorerColumn] = [
    # ── Box totals (always-on, shown) ──────────────────────────────────────
    # Raw counting stats sum across pools exactly (additive).
    ExplorerColumn(
        "gp",
        "GP",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="int",
        shown=True,
    ),
    ExplorerColumn(
        "min",
        "MIN",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "pts",
        "PTS",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "reb",
        "REB",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "ast",
        "AST",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "stl",
        "STL",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "blk",
        "BLK",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "tov",
        "TOV",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "oreb",
        "OREB",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "dreb",
        "DREB",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "pf",
        "PF",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "plus_minus",
        "+/-",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    # ── Shooting stats (always-on, shown) ─────────────────────────────────
    # eFG% is box-derived (recombinable) and lives in the always-on base columns.
    ExplorerColumn(
        "efg_pct",
        "eFG%",
        _GROUP_SHOOTING,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "fgm",
        "FGM",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "fga",
        "FGA",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "fg3m",
        "3PM",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "fg3a",
        "3PA",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "ftm",
        "FTM",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "fta",
        "FTA",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "fg_pct",
        "FG%",
        _GROUP_SHOOTING,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "fg3_pct",
        "3P%",
        _GROUP_SHOOTING,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "ft_pct",
        "FT%",
        _GROUP_SHOOTING,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    # ── Advanced (shown only in single-competition per_competition view) ───
    # TS% is box-derived (recombinable) but reads as an advanced efficiency metric,
    # so it is surfaced with the composites rather than the always-on shooting columns.
    ExplorerColumn(
        "ts_pct",
        "TS%",
        _GROUP_ADVANCED,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    # Attempt rates (3PAr, FTr) are box-derived like TS% — recombinable, exact at
    # any grain. Stored/displayed as 0-1 fractions at 3 decimals (BBRef-style),
    # matching the Leaders board and player-page advanced table.
    ExplorerColumn(
        "fg3ar",
        "3PAr",
        _GROUP_ADVANCED,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="f3",
        shown=True,
    ),
    ExplorerColumn(
        "ftr",
        "FTr",
        _GROUP_ADVANCED,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="f3",
        shown=True,
    ),
    # Assisted share of made FGs, from PBP-derived counts (ast_fgm/unast_fgm).
    # Recombinable — counts sum exactly across pools; NULL outside the PBP era.
    ExplorerColumn(
        "astd_pct",
        "AST'd%",
        _GROUP_ADVANCED,
        _BUCKET_RECOMBINABLE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    # Rebound / defensive participation rates (stored 0-100): exact within one
    # pool; pooled minute-weighted (approximate, "avg"-marked) at career grain.
    ExplorerColumn(
        "orb_pct",
        "ORB%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "drb_pct",
        "DRB%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "trb_pct",
        "TRB%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "ast_pct",
        "AST%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "stl_pct",
        "STL%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "blk_pct",
        "BLK%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "tov_pct",
        "TOV%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    ExplorerColumn(
        "usg_pct",
        "USG%",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="pct",
        shown=True,
    ),
    # Pool-calibrated composites: minute-weighted (approximate, "avg"-marked)
    # across pools at career grain; exact within one competition.
    ExplorerColumn(
        "per",
        "PER",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "ortg",
        "ORtg",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "drtg",
        "DRtg",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "net_rtg",
        "NRtg",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "obpm",
        "OBPM",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "dbpm",
        "DBPM",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "bpm",
        "BPM",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    # Cumulative shares (and their components) sum exactly across pools.
    ExplorerColumn(
        "ows",
        "OWS",
        _GROUP_ADVANCED,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "dws",
        "DWS",
        _GROUP_ADVANCED,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "ws",
        "WS",
        _GROUP_ADVANCED,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    # ws40: WS per 40 minutes. rate_composite — and the minute-weighted mean of
    # 40·WS/min IS 40·ΣWS/Σmin, so the career aggregate is exact, not approximate.
    ExplorerColumn(
        "ws40",
        "WS/40",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f2",
        shown=True,
    ),
    # ws82 / vorp82 are per-season *rates* (projections), not accumulations:
    # summing them would triple-count a three-pool career, so they pool
    # minute-weighted like the other rate composites (matching the player-page
    # career row and the Leaders blend).
    ExplorerColumn(
        "ws82",
        "WS/82",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "vorp",
        "VORP",
        _GROUP_ADVANCED,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    ExplorerColumn(
        "vorp82",
        "VORP/82",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=True,
        filterable=True,
        fmt="f1",
        shown=True,
    ),
    # ── Advanced (catalog-classified; deliberately not shown) ──────────────
    # pts_per100 duplicates the Per-100 display mode as a column, and pace is a
    # pool/team attribute rather than a player skill — both stay catalog-only.
    ExplorerColumn(
        "pts_per100",
        "Pts/100",
        _GROUP_ADVANCED,
        _BUCKET_RECOMBINABLE,
        sortable=False,
        filterable=False,
        fmt="f1",
        shown=False,
    ),
    ExplorerColumn(
        "pace",
        "Pace",
        _GROUP_ADVANCED,
        _BUCKET_RATE_COMPOSITE,
        sortable=False,
        filterable=False,
        fmt="f1",
        shown=False,
    ),
    # Game Score is additive/linear in the box stats (not pool-recalibrated like
    # PER/BPM), so it is exact at every grain and lives with the base columns
    # (shown + sortable), per #426. Value is recomputed from box stats in
    # _compute_player_values via game_score_from_row.
    ExplorerColumn(
        "gmsc",
        "GmSc",
        _GROUP_BOX,
        _BUCKET_ADDITIVE,
        sortable=True,
        filterable=False,
        fmt="f1",
        shown=True,
    ),
]

# Derived views for backward compatibility with existing callers.
# The catalog is the single source of truth; these lists are window views.

# Columns shown in the always-on players table (box + shooting groups, shown=True).
_PLAYER_STAT_COLUMNS: list[ExplorerColumn] = [
    c for c in PLAYER_COLUMN_CATALOG if c.shown and c.group != _GROUP_ADVANCED
]

# Advanced columns exposed in single-competition per_competition view (shown=True, advanced group).
# TS% leads the group (box-derived but reads as advanced); composites follow.
# These must NOT be mixed into career / multi-competition rows.
_PLAYER_ADVANCED_COLUMNS: list[ExplorerColumn] = [
    c for c in PLAYER_COLUMN_CATALOG if c.shown and c.group == _GROUP_ADVANCED
]

# Stat columns for the teams subject (one row per team-season). W-L and points
# come from game scores; pace/ratings are team-box-log averages where present.
# net_rtg = ORtg - DRtg; None when either component is unavailable.
_TEAM_STAT_COLUMNS: list[ExplorerColumn] = [
    ExplorerColumn("gp", "GP"),
    ExplorerColumn("w", "W"),
    ExplorerColumn("l", "L"),
    ExplorerColumn("ppg", "PPG"),
    ExplorerColumn("opp_ppg", "OPP"),
    ExplorerColumn("diff", "DIFF"),
    # Advanced team metrics (pace + ratings) — grouped so the column-group toggle
    # can show/hide them alongside the player advanced columns.
    ExplorerColumn("pace", "PACE", _GROUP_ADVANCED),
    ExplorerColumn("ortg", "ORtg", _GROUP_ADVANCED),
    ExplorerColumn("drtg", "DRtg", _GROUP_ADVANCED),
    ExplorerColumn("net_rtg", "NetRtg", _GROUP_ADVANCED),
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
# Players sort keys: generated from the catalog's sortable flag.
# Covers box, shooting, and advanced (ts_pct, per, ortg, drtg, bpm, ws, vorp) columns.
_SORT_KEYS_BY_SUBJECT["players"] = {c.key for c in PLAYER_COLUMN_CATALOG if c.sortable}
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
# Advanced composite keys not available in the per_game SELECT (game-log rows have no
# pre-computed composite metrics).  Used by parse_query to coerce invalid per_game sorts.
_ADV_COMPOSITE_SORT_KEYS: frozenset[str] = frozenset(
    c.key
    for c in PLAYER_COLUMN_CATALOG
    if c.group == _GROUP_ADVANCED and c.sortable and c.key != "ts_pct"
)


# --------------------------------------------------------------------------- #
# Roll-up primitives
#
# Each function accepts a sequence of per-competition rows (objects with
# numeric attributes — typically SummerLeaguePlayerSeason instances) and folds
# them into one pooled value for the requested column key.  Rows whose value is
# None are always skipped (they contribute no weight or sum).
#
# Three buckets, three functions — driven by the catalog's ``bucket`` field:
#
#   recombinable   → rollup_recombinable   (sum box components, recompute ratio)
#   additive       → rollup_additive       (sum values, skip None)
#   rate_composite → rollup_rate_composite (minute-weighted avg, skip None pools)
#
# All percentage columns are stored as percentages (e.g. 60.6 not 0.606) and
# the roll-up output preserves that convention.
# --------------------------------------------------------------------------- #


def _sum_attr(rows: Sequence[Any], attr: str) -> float:
    """Sum a numeric attribute across rows, treating None / missing as zero."""
    return sum(float(getattr(r, attr, None) or 0) for r in rows)


def rollup_additive(rows: Sequence[Any], key: str) -> Optional[float]:
    """Sum ``key`` across competition rows; returns ``None`` when all values are None.

    Null-safe: rows where ``getattr(row, key)`` is ``None`` are skipped.  If every
    row is ``None`` (no data at all) the function returns ``None`` rather than 0 to
    distinguish "no data" from "zero total".

    Args:
        rows: Per-competition rows with numeric attributes.
        key:  Attribute name to sum (e.g. ``"ws"``, ``"vorp"``, ``"pts"``).

    Returns:
        Summed value, or ``None`` when all rows have ``None`` for ``key``.
    """
    if not rows:
        return None
    vals = [getattr(r, key, None) for r in rows]
    if all(v is None for v in vals):
        return None
    return sum(float(v) for v in vals if v is not None)


def rollup_rate_composite(rows: Sequence[Any], key: str) -> Optional[float]:
    """Minute-weighted average of ``key`` across competition pools; skips null pools.

    Ineligible pools (``None`` value or zero/negative minutes) are excluded from
    both numerator and denominator so they do not drag the average toward zero.
    Returns ``None`` when no eligible pool has sufficient data.

    Args:
        rows: Per-competition rows; each must have a ``minutes`` attribute.
        key:  Attribute name (e.g. ``"per"``, ``"bpm"``, ``"usg_pct"``).

    Returns:
        Minute-weighted mean, or ``None`` when no eligible pool exists.
    """
    num = 0.0
    den = 0.0
    for r in rows:
        val = getattr(r, key, None)
        mins = float(getattr(r, "minutes", 0) or 0)
        if val is None or mins <= 0:
            continue  # skip ineligible pool (missing composite or sub-zero minutes)
        num += float(val) * mins
        den += mins
    return num / den if den else None


def rollup_recombinable(rows: Sequence[Any], key: str) -> Optional[float]:
    """Recompute ``key`` from summed box components (exact at any grain).

    Summing per-competition values of a ratio metric (e.g. averaging TS% rows)
    is inexact because small pools are over-weighted.  This function sums the
    underlying box totals first and then applies the ratio formula once, which
    gives the same result as computing the metric over the full multi-competition
    box sheet.

    Supported keys: ``ts_pct``, ``efg_pct``, ``fg_pct``, ``fg3_pct``, ``ft_pct``,
    ``fg3ar``, ``ftr``, ``pts_per100``.

    Args:
        rows: Per-competition rows with box-total attributes (``pts``, ``fgm``, …).
        key:  Recombinable metric key.

    Returns:
        Recomputed metric value (stored as a percentage, e.g. 60.6), or ``None``
        when the denominator sums to zero (no attempts).
    """
    if not rows:
        return None

    fgm = _sum_attr(rows, "fgm")
    fga = _sum_attr(rows, "fga")
    fg3m = _sum_attr(rows, "fg3m")
    fg3a = _sum_attr(rows, "fg3a")
    ftm = _sum_attr(rows, "ftm")
    fta = _sum_attr(rows, "fta")
    pts = _sum_attr(rows, "pts")

    if key == "ts_pct":
        denom = 2.0 * (fga + 0.44 * fta)
        return 100.0 * pts / denom if denom else None
    if key == "efg_pct":
        return 100.0 * (fgm + 0.5 * fg3m) / fga if fga else None
    if key == "fg_pct":
        return 100.0 * fgm / fga if fga else None
    if key == "fg3_pct":
        return 100.0 * fg3m / fg3a if fg3a else None
    if key == "ft_pct":
        return 100.0 * ftm / fta if fta else None
    if key == "fg3ar":
        # 3-point attempt rate: share of field-goal attempts that are 3-pointers.
        # 0-1 fraction (BBRef scale), matching the stored season column.
        return fg3a / fga if fga else None
    if key == "ftr":
        # Free-throw rate: FTA per FGA (ability to draw fouls). 0-1 fraction.
        return fta / fga if fga else None
    if key == "astd_pct":
        # Assisted share of made FGs from PBP counts; None outside the PBP era.
        ast_fgm = _sum_attr(rows, "ast_fgm")
        unast_fgm = _sum_attr(rows, "unast_fgm")
        made = ast_fgm + unast_fgm
        return 100.0 * ast_fgm / made if made else None
    if key == "pts_per100":
        # ``pace`` is possessions per 48 minutes (NBA's normalization base, kept even
        # for 40-minute Summer League games — see summer_league.constants). Sum
        # pace×minutes over pace-covered competitions, then extrapolate to all minutes
        # using the minute-weighted observed pace so full-career points aren't divided
        # by only the covered possessions (which would explode the rate across the 2017
        # pace boundary). Matches the career per_100 mode's pace_sec extrapolation.
        covered_min = sum(
            float(getattr(r, "minutes", 0) or 0)
            for r in rows
            if getattr(r, "pace", None) is not None
        )
        if not covered_min:
            return None
        total_min = sum(float(getattr(r, "minutes", 0) or 0) for r in rows)
        pace_min_sum = sum(
            float(getattr(r, "pace", None) or 0) * float(getattr(r, "minutes", 0) or 0)
            for r in rows
        )
        poss = (pace_min_sum / MINUTES_PER_GAME) * (total_min / covered_min)
        return 100.0 * pts / poss if poss else None
    # Unknown key — callers should only pass recombinable keys from the catalog.
    return None


# --------------------------------------------------------------------------- #
# Metric threshold filter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MetricFilter:
    """A validated metric threshold filter for the Explorer.

    Attributes:
        col:   Catalog column key with filterable=True.
        op:    Operator: ``">="`` or ``"<="``; URL form ``"gte"``/``"lte"`` is mapped
               during parsing.
        value: Numeric threshold.
    """

    col: str
    op: str  # ">=" | "<="
    value: float


# Derived set of filterable column keys (from catalog).
_FILTERABLE_KEYS: frozenset[str] = frozenset(
    c.key for c in PLAYER_COLUMN_CATALOG if c.filterable
)

# A player-game row carries only the traditional box score. These filters can
# be evaluated exactly from that one line; pool-calibrated composites and
# team/PBP-context rates cannot. Keeping this vocabulary explicit prevents a
# game-finder URL from appearing to apply a predicate that the data cannot
# actually answer.
_PER_GAME_UNSUPPORTED_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "gp",
        "per",
        "ortg",
        "drtg",
        "net_rtg",
        "obpm",
        "dbpm",
        "bpm",
        "ws",
        "ows",
        "dws",
        "ws40",
        "ws82",
        "vorp",
        "vorp82",
        "astd_pct",
        "usg_pct",
        "ast_pct",
        "orb_pct",
        "drb_pct",
        "trb_pct",
        "stl_pct",
        "blk_pct",
    }
)
_PER_GAME_FILTERABLE_KEYS: frozenset[str] = (
    _FILTERABLE_KEYS - _PER_GAME_UNSUPPORTED_FILTER_KEYS
)
PER_GAME_FILTERABLE_COLUMNS: list[ExplorerColumn] = [
    c for c in PLAYER_COLUMN_CATALOG if c.key in _PER_GAME_FILTERABLE_KEYS
]


def parse_metric_filters(params: dict[str, str]) -> list[MetricFilter]:
    """Parse ``fcol0/fop0/fval0`` … ``fcol2/fop2/fval2`` into validated filters.

    Accepts up to 3 indexed filter rows. Each filter needs a filterable catalog
    key (``fcol{i}``), a valid operator (``fop{i}``: ``"gte"`` or ``"lte"``), and a
    numeric threshold (``fval{i}``). Invalid predicates are silently dropped —
    this function never raises; other valid filters still apply.

    Args:
        params: Raw URL query-string params (one value per key).

    Returns:
        List of up to 3 validated :class:`MetricFilter` instances (may be empty).
    """
    _OP_MAP = {"gte": ">=", "lte": "<="}
    filters: list[MetricFilter] = []
    for i in range(3):
        col = params.get(f"fcol{i}", "").strip()
        op_raw = params.get(f"fop{i}", "").strip()
        val_str = params.get(f"fval{i}", "").strip()
        if not col or not op_raw or not val_str:
            continue
        if col not in _FILTERABLE_KEYS:
            continue
        op = _OP_MAP.get(op_raw)
        if op is None:
            continue
        try:
            value = float(val_str)
        except ValueError:
            continue
        filters.append(MetricFilter(col=col, op=op, value=value))
    return filters


def _career_metric_having(f: MetricFilter, ps: Any) -> Any:
    """HAVING expression for a metric filter at career grain.

    Filters are applied to SQL aggregate expressions, not mode-scaled display
    rates. Counting stats (pts/reb/etc.) filter on career totals (SUM) for
    mode-independent URL round-trips — ``fcol0=pts&fval0=40`` means "40 total
    career points" regardless of the selected display mode. Percentage and
    advanced metrics are mode-independent by nature.
    """

    def _op(expr: Any) -> Any:
        return expr >= f.value if f.op == ">=" else expr <= f.value

    col = f.col
    # Rate composites: minute-weighted average (mirrors _rate_composite_agg).
    if col in (
        "per",
        "ortg",
        "drtg",
        "net_rtg",
        "obpm",
        "dbpm",
        "bpm",
        "usg_pct",
        "ast_pct",
        "tov_pct",
        "orb_pct",
        "drb_pct",
        "trb_pct",
        "stl_pct",
        "blk_pct",
        "ws40",
        "ws82",
        "vorp82",
    ):
        attr = getattr(ps, col)
        eligible_min = func.sum(  # type: ignore[attr-defined]
            case((attr.isnot(None), ps.minutes), else_=literal(0))  # type: ignore[attr-defined]
        )
        return _op(func.sum(attr * ps.minutes) / func.nullif(eligible_min, 0))  # type: ignore[attr-defined]
    # Additive advanced (exact sum).
    if col in ("ws", "ows", "dws", "vorp"):
        return _op(func.sum(getattr(ps, col)))  # type: ignore[attr-defined]
    # Box counting stats — filter on career totals.
    if col in (*_COUNTING, "plus_minus", "gp"):
        return _op(func.sum(getattr(ps, col)))  # type: ignore[attr-defined]
    # Minutes (career total in minutes).
    if col == "min":
        return _op(func.sum(ps.minutes))  # type: ignore[attr-defined]
    # Recombinable percentage metrics — pool ratio from summed box components.
    if col == "efg_pct":
        return _op(
            100.0
            * (func.sum(ps.fgm) + 0.5 * func.sum(ps.fg3m))  # type: ignore[attr-defined]
            / func.nullif(func.sum(ps.fga), 0)  # type: ignore[attr-defined]
        )
    if col == "fg_pct":
        return _op(100.0 * func.sum(ps.fgm) / func.nullif(func.sum(ps.fga), 0))  # type: ignore[attr-defined]
    if col == "fg3_pct":
        return _op(100.0 * func.sum(ps.fg3m) / func.nullif(func.sum(ps.fg3a), 0))  # type: ignore[attr-defined]
    if col == "ft_pct":
        return _op(100.0 * func.sum(ps.ftm) / func.nullif(func.sum(ps.fta), 0))  # type: ignore[attr-defined]
    if col == "ts_pct":
        denom = 2.0 * (func.sum(ps.fga) + 0.44 * func.sum(ps.fta))  # type: ignore[attr-defined]
        return _op(100.0 * func.sum(ps.pts) / func.nullif(denom, 0))  # type: ignore[attr-defined]
    # Attempt rates — 0-1 fraction ratios from summed box (thresholds on the
    # displayed fraction scale, e.g. fval=0.4 means FTr ≥ .400).
    if col == "fg3ar":
        return _op(
            func.sum(ps.fg3a) * 1.0 / func.nullif(func.sum(ps.fga), 0)  # type: ignore[attr-defined]
        )
    if col == "ftr":
        return _op(
            func.sum(ps.fta) * 1.0 / func.nullif(func.sum(ps.fga), 0)  # type: ignore[attr-defined]
        )
    # Assisted share of made FGs (0-100 scale) from summed PBP counts.
    if col == "astd_pct":
        made = func.sum(ps.ast_fgm) + func.sum(ps.unast_fgm)  # type: ignore[attr-defined]
        return _op(100.0 * func.sum(ps.ast_fgm) / func.nullif(made, 0))  # type: ignore[attr-defined]
    return None


def _per_comp_metric_where(f: MetricFilter, ps: Any) -> Any:
    """WHERE condition for a metric filter at per_competition grain.

    Each row is one competition's season totals. Filters apply to raw season
    column values, not mode-scaled display rates.
    """

    def _op(expr: Any) -> Any:
        return expr >= f.value if f.op == ">=" else expr <= f.value

    col = f.col
    # Stored composite values: filter directly on the season column.
    if col in (
        "per",
        "ortg",
        "drtg",
        "net_rtg",
        "obpm",
        "dbpm",
        "bpm",
        "ws",
        "ows",
        "dws",
        "ws40",
        "ws82",
        "vorp",
        "vorp82",
        "usg_pct",
        "ast_pct",
        "tov_pct",
        "orb_pct",
        "drb_pct",
        "trb_pct",
        "stl_pct",
        "blk_pct",
    ):
        return _op(getattr(ps, col))
    # Box counting season totals.
    if col in (*_COUNTING, "plus_minus", "gp"):
        return _op(getattr(ps, col))
    # Minutes stored as minutes in the season table.
    if col == "min":
        return _op(ps.minutes)
    # Recombinable percentages from season box components.
    if col == "efg_pct":
        return _op(100.0 * (ps.fgm + 0.5 * ps.fg3m) / func.nullif(ps.fga, 0))  # type: ignore[attr-defined]
    if col == "fg_pct":
        return _op(100.0 * ps.fgm / func.nullif(ps.fga, 0))  # type: ignore[attr-defined]
    if col == "fg3_pct":
        return _op(100.0 * ps.fg3m / func.nullif(ps.fg3a, 0))  # type: ignore[attr-defined]
    if col == "ft_pct":
        return _op(100.0 * ps.ftm / func.nullif(ps.fta, 0))  # type: ignore[attr-defined]
    if col == "ts_pct":
        denom = 2.0 * (ps.fga + 0.44 * ps.fta)
        return _op(100.0 * ps.pts / func.nullif(denom, 0))  # type: ignore[attr-defined]
    # Attempt rates — 0-1 fraction ratios from the row's box components.
    if col == "fg3ar":
        return _op(ps.fg3a * 1.0 / func.nullif(ps.fga, 0))  # type: ignore[attr-defined]
    if col == "ftr":
        return _op(ps.fta * 1.0 / func.nullif(ps.fga, 0))  # type: ignore[attr-defined]
    # Assisted share of made FGs (0-100 scale) from the row's PBP counts.
    if col == "astd_pct":
        made = ps.ast_fgm + ps.unast_fgm
        return _op(100.0 * ps.ast_fgm / func.nullif(made, 0))  # type: ignore[attr-defined]
    return None


def _per_game_metric_where(f: MetricFilter, pgl: Any) -> Any:
    """WHERE condition for a metric filter at per_game grain.

    Each row is a single game log. The UI and query parser exclude advanced
    composites (PER/ORtg/BPM/WS/VORP) plus team/PBP-context rates because they
    are not stored on a game log. The ``None`` fallback remains defensive for
    programmatic callers.
    """

    def _op(expr: Any) -> Any:
        return expr >= f.value if f.op == ">=" else expr <= f.value

    col = f.col
    # Per-game box stats (raw game log values — the displayed value in per_game mode).
    if col in (*_COUNTING, "plus_minus"):
        return _op(getattr(pgl, col))
    # gp per row is always 1 — filter not meaningful; skip.
    if col == "gp":
        return None
    # Minutes per game: minutes_seconds / 60.
    if col == "min":
        return _op(pgl.minutes_seconds / 60.0)  # type: ignore[attr-defined]
    # Percentage metrics from game-log box columns.
    if col == "efg_pct":
        return _op(
            100.0 * (pgl.fgm + 0.5 * pgl.fg3m) / func.nullif(pgl.fga, 0)  # type: ignore[attr-defined]
        )
    if col == "fg_pct":
        return _op(100.0 * pgl.fgm / func.nullif(pgl.fga, 0))  # type: ignore[attr-defined]
    if col == "fg3_pct":
        return _op(100.0 * pgl.fg3m / func.nullif(pgl.fg3a, 0))  # type: ignore[attr-defined]
    if col == "ft_pct":
        return _op(100.0 * pgl.ftm / func.nullif(pgl.fta, 0))  # type: ignore[attr-defined]
    if col == "ts_pct":
        denom = 2.0 * (pgl.fga + 0.44 * pgl.fta)
        return _op(100.0 * pgl.pts / func.nullif(denom, 0))  # type: ignore[attr-defined]
    # Box-derived rates work per game from the row's own line (thresholds on
    # the same scale the other grains use: 0-1 fractions for attempt rates,
    # 0-100 for TOV%).
    if col == "fg3ar":
        return _op(pgl.fg3a * 1.0 / func.nullif(pgl.fga, 0))  # type: ignore[attr-defined]
    if col == "ftr":
        return _op(pgl.fta * 1.0 / func.nullif(pgl.fga, 0))  # type: ignore[attr-defined]
    if col == "tov_pct":
        plays = pgl.fga + 0.44 * pgl.fta + pgl.tov
        return _op(100.0 * pgl.tov / func.nullif(plays, 0))  # type: ignore[attr-defined]
    # Advanced composites and team/PBP-context rates (USG%, AST%, AST'd%,
    # rebound/steal/block %s) are not derivable per game log — silently skip.
    return None


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
    # Nth summer-league appearance to isolate (1..3 = exact; APPEARANCE_TOP = 4th+).
    # None means no filter. See APPEARANCE_TOP / _apply_appearance_filter.
    appearance: Optional[int] = None
    country: Optional[str] = None
    team_slug: Optional[str] = None
    round_type: Optional[str] = None
    undrafted: bool = False
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    min_games: int = DEFAULT_MIN_GAMES
    min_minutes: int = DEFAULT_MIN_MINUTES
    mode: str = DEFAULT_MODE
    sort: str = "pts"
    direction: str = "desc"
    page: int = 1
    # Metric threshold filters (up to 3); parsed from fcol{i}/fop{i}/fval{i} params.
    metric_filters: list[MetricFilter] = field(default_factory=list)
    # When False, query builders skip LIMIT/OFFSET and return every matching
    # row (used by the CSV export so downloads are not capped to one page).
    paginate: bool = True
    # Internal: when set, per_competition queries filter results to this single
    # player slug.  Not a user-facing URL param — set programmatically for
    # the drill-down endpoint so it can reuse ``_query_players_per_competition``.
    player_slug: Optional[str] = None


@dataclass
class ExplorerRow:
    """One result row: a label cell (with optional link) plus stat values."""

    label: str
    href: Optional[str]
    values: dict[str, Any]
    # True when this row's per_100 rates are approximate: the career pool mixes
    # pace-covered and pace-missing (pre-2017) competitions, so possessions are
    # only partially known. Career grain + per_100 mode only.
    per100_approx: bool = False


@dataclass
class ExplorerFacets:
    """Choices offered by the query-builder panel."""

    years: list[int] = field(default_factory=list)
    venues: list[tuple[str, str]] = field(default_factory=list)
    draft_classes: list[int] = field(default_factory=list)
    # (value, label) pairs: slot positions ("pg" → "PG") and broader groups
    # ("guard" → "Guards"), rendered as separate optgroups in one dropdown.
    positions: list[tuple[str, str]] = field(default_factory=list)
    position_groups: list[tuple[str, str]] = field(default_factory=list)
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
    adv_eligible: bool = False
    # Keys of result columns whose values are minute-weighted pooled averages (career grain).
    # These are rate_composite columns that span multiple competition pools; shown with
    # an "≈ avg" marker in the UI to signal they are approximate aggregates.
    pooled_composite_keys: frozenset[str] = field(default_factory=frozenset)
    # N-of-M counts for the eligibility banner.
    adv_eligible_n: int = 0  # competitions in scope with adv_eligible=True
    adv_eligible_m: int = 0  # total competitions in scope with a metric context row


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
    # Position values come from a fixed vocabulary (slots + buckets); anything
    # else (stale bookmarks, hand-typed params) degrades to no filter.
    position: Optional[str] = (params.get("position") or "").lower() or None
    if position is not None and position not in _POSITION_FILTER_VALUES:
        position = None
    # Nth summer-league appearance to isolate. Valid values are 1..APPEARANCE_TOP
    # (4 = "4th+"); anything outside that range (blank, 0, negatives, stray large
    # values) degrades to no filter.
    appearance = _to_int(params.get("appearance"))
    if appearance is not None and not (APPEARANCE_MIN <= appearance <= APPEARANCE_TOP):
        appearance = None
    # Canonicalize so a raw ?country=US URL resolves to the same value the
    # dropdown emits ("United States") and matches every stored encoding.
    country = canonical_country(params.get("country"))
    team_slug = params.get("team_slug") or None
    round_type = params.get("round_type") or None
    undrafted = params.get("undrafted") == "1"

    year_min = _to_int(params.get("year_min"))
    year_max = _to_int(params.get("year_max"))

    # Advanced composite sort keys (PER/ORtg/DRtg/BPM/WS/VORP) are computed by the
    # career and per_competition queries but NOT available in the per_game SELECT
    # (individual game logs have no pre-computed composite metrics).
    # Coerce to the default only when grain=per_game to prevent ORDER BY on a missing column.
    # ts_pct stays valid everywhere — it is box-derived (recombinable).
    if grain == "per_game" and sort in _ADV_COMPOSITE_SORT_KEYS:
        sort = default_sort

    # Reject implausible future draft classes so a hand-typed ?draft_class=2033
    # cannot filter on a phantom year (mirrors the facet clamp).
    draft_class = _to_int(params.get("draft_class"))
    if draft_class is not None and draft_class > _max_plausible_draft_class():
        draft_class = None

    min_games = _to_int(params.get("min_gp"))
    min_minutes = _to_int(params.get("min_min"))
    page = _to_int(params.get("page")) or 1
    metric_filters = parse_metric_filters(params)
    if grain == "per_game":
        metric_filters = [
            metric_filter
            for metric_filter in metric_filters
            if metric_filter.col in _PER_GAME_FILTERABLE_KEYS
        ]

    return ExplorerQuery(
        subject=subject,
        grain=grain,
        year_min=year_min,
        year_max=year_max,
        venue=venue,
        draft_class=draft_class,
        draft_round=_to_int(params.get("draft_round")),
        draft_pick_min=_to_int(params.get("draft_pick_min")),
        draft_pick_max=_to_int(params.get("draft_pick_max")),
        position=position,
        appearance=appearance,
        country=country,
        team_slug=team_slug,
        round_type=round_type,
        undrafted=undrafted,
        age_min=_to_int(params.get("age_min")),
        age_max=_to_int(params.get("age_max")),
        min_games=min_games if min_games is not None else DEFAULT_MIN_GAMES,
        min_minutes=min_minutes if min_minutes is not None else DEFAULT_MIN_MINUTES,
        mode=mode,
        sort=sort,
        direction=direction,
        page=max(1, page),
        metric_filters=metric_filters,
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
                # Clamp out implausible future classes (data artifacts) so the
                # dropdown never lists phantom years like 2030+.
                .where(PlayerMaster.draft_year <= _max_plausible_draft_class())  # type: ignore[operator]
                .distinct()
                .order_by(PlayerMaster.draft_year.desc())  # type: ignore[union-attr]
            )
        ).all()
    ]
    # Position facet: position codes referenced by players in the SL pool.
    # Semi-join (IN subqueries) rather than join+DISTINCT so the planner never
    # fans out over the per-player-per-competition season rows; positions.id
    # is the PK, so the result is already unique. Hybrid codes are split into
    # their slot components ("_" is the fine-token delimiter written by
    # derive_position_tags); parent buckets come from the same taxonomy.
    pos_codes = [
        str(code)
        for (code,) in (
            await db.execute(
                select(Position.code).where(  # type: ignore[call-overload]
                    Position.id.in_(  # type: ignore[union-attr]
                        select(PlayerStatus.position_id).where(  # type: ignore[call-overload]
                            PlayerStatus.player_id.in_(  # type: ignore[attr-defined]
                                select(SummerLeaguePlayerSeason.player_id)  # type: ignore[call-overload]
                            )
                        )
                    )
                )
            )
        ).all()
    ]
    slot_set: set[str] = set()
    bucket_set: set[str] = set()
    for code in pos_codes:
        slot_set.update(code.split("_"))
        bucket_set.update(get_parents_for_fine(code))
    positions = [(s, s.upper()) for s in _POSITION_SLOTS if s in slot_set]
    position_groups = [
        (b, _POSITION_BUCKET_LABELS[b]) for b in _POSITION_BUCKETS if b in bucket_set
    ]
    # Raw birth_country mixes ISO-2 codes, full names, and aliases across
    # ingestion sources.  Normalize each to a canonical display name, then
    # dedupe + sort so the dropdown lists every country exactly once.
    countries = sorted(
        {
            name
            for (c,) in (
                await db.execute(
                    select(PlayerMaster.birth_country)  # type: ignore[call-overload]
                    .where(PlayerMaster.birth_country.isnot(None))  # type: ignore[union-attr]
                    .distinct()
                )
            ).all()
            if (name := canonical_country(c)) is not None
        }
    )
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
        position_groups=position_groups,
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

    # Game Score is linear in the box stats, so scaling the summed-box Game Score
    # by the same per-mode factor is exact: per_game → mean per-game GmSc (matches
    # the materialized season value), totals → cumulative, per_36/per_100 → rate.
    gmsc_total = game_score_from_row(r)

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
        # Attempt rates: 0-1 fractions at 3 decimals (recombinable — exact from
        # the summed box at any grain, like the shooting percentages above).
        "fg3ar": (round(float(r.fg3a or 0) / fga, 3) if fga else None),
        "ftr": (round(fta / fga, 3) if fga else None),
        "astd_pct": _astd_pct(r),
        "gmsc": scaled(gmsc_total),
    }


def _astd_pct(r: Any) -> Optional[float]:
    """Assisted share of made FGs from PBP counts; ``None`` outside the PBP era."""
    ast_fgm = getattr(r, "ast_fgm", None)
    unast_fgm = getattr(r, "unast_fgm", None)
    made = float(ast_fgm or 0) + float(unast_fgm or 0)
    if made <= 0:
        return None
    return round(100.0 * float(ast_fgm or 0) / made, 1)


def _scaled_sort_expr(num: str, gp: str, sec: str, pace_sec: str, mode: str) -> str:
    """Scale a counting-stat numerator into the displayed per-mode rate.

    Mirrors the arithmetic in :func:`_compute_player_values` so ORDER BY ranks on
    exactly what the cell shows.  ``num``/``gp``/``sec``/``pace_sec`` are SQL
    fragments (aggregates for career, raw labels for per_competition); ``sec`` is
    seconds played and ``pace_sec`` the pace-weighted seconds.
    """
    if mode == "per_game":
        # * 1.0 forces float division (counts/totals are integers in Postgres,
        # and integer division would truncate the rate into non-monotonic ties).
        return f"{num} * 1.0 / NULLIF({gp}, 0)"
    if mode == "per_36":  # 36 min / (sec/60) = num * 36 * 60 / sec
        return f"{num} * 2160.0 / NULLIF({sec}, 0)"
    if mode == "per_100":  # 100 poss; poss = pace_sec / (60 * 48)
        return f"{num} * 288000.0 / NULLIF({pace_sec}, 0)"
    return num  # totals


def _game_score_sql(box: Callable[[str], str]) -> str:
    """Build the Hollinger Game Score expression in SQL.

    ``box`` maps a column name to its SQL fragment so the same formula serves
    both the career grain (``SUM(pts)`` …) and the per-competition / per-game
    grains (raw labels ``pts`` …). Mirrors :func:`game_score` exactly so an
    ORDER BY on GmSc ranks on the same value the cell shows (before per-mode
    scaling, which the caller layers on via :func:`_scaled_sort_expr`).
    """
    return (
        f"({box('pts')} + 0.4 * {box('fgm')} - 0.7 * {box('fga')} "
        f"- 0.4 * ({box('fta')} - {box('ftm')}) + 0.7 * {box('oreb')} "
        f"+ 0.3 * {box('dreb')} + {box('stl')} + 0.7 * {box('ast')} "
        f"+ 0.7 * {box('blk')} - 0.4 * {box('pf')} - {box('tov')})"
    )


# GmSc numerator fragments: raw column labels (per_competition / per_game) and
# SUM aggregates (career).  Scaled into the displayed per-mode rate at call time.
# Each component is COALESCEd to 0 so a NULL box stat (e.g. unrecorded OREB on an
# older log) does not poison the whole expression to NULL — matching the Python
# display path, which coalesces None to 0 via :func:`game_score_line`.
_GMSC_SQL_RAW = _game_score_sql(lambda c: f"COALESCE({c}, 0)")
_GMSC_SQL_AGG = _game_score_sql(lambda c: f"COALESCE(SUM({c}), 0)")

# Career-grain per_100 pace-weighted seconds: pace-covered possessions extrapolated
# to all minutes via the minute-weighted observed pace (mirrors pace_sec_expr in
# _query_players). Keeps counting-stat sorts monotonic with the displayed per_100 cell.
_CAREER_PACE_SEC_SQL = (
    "SUM(pace * minutes) * 60 * SUM(minutes) "
    "/ NULLIF(SUM(CASE WHEN pace IS NOT NULL THEN minutes ELSE 0 END), 0)"
)


def _player_sort_expr(sort_col: str, mode: str) -> Any:
    """Return a SQL text expression used in ORDER BY for the players career grain.

    Counting stats sort on their **displayed per-mode rate** (computed in SQL by
    repeating the SUM aggregates) so the sorted column is visually monotonic in
    the selected mode — sorting on the raw SUM is only monotonic with totals, and
    reads as "broken sort" in per-game/-36/-100.  Minutes sort on per-game minutes
    (or total minutes in totals mode).  Percentage stats use NULLIF-guarded ratios.

    Aggregates are repeated (e.g. ``SUM(pts)``) rather than referencing the SELECT
    aliases because Postgres resolves names inside an ORDER BY *expression* against
    input columns, not output labels.
    """
    # Percentage stats — mode-independent NULLIF-guarded ratio expressions.
    _pct_exprs: dict[str, str] = {
        "efg_pct": "(SUM(fgm) + 0.5 * SUM(fg3m)) / NULLIF(SUM(fga), 0)",
        "fg_pct": "SUM(fgm) * 1.0 / NULLIF(SUM(fga), 0)",
        "fg3_pct": "SUM(fg3m) * 1.0 / NULLIF(SUM(fg3a), 0)",
        "ft_pct": "SUM(ftm) * 1.0 / NULLIF(SUM(fta), 0)",
        "ts_pct": "SUM(pts) / NULLIF(2.0 * (SUM(fga) + 0.44 * SUM(fta)), 0)",
    }
    if sort_col in _pct_exprs:
        return _pct_exprs[sort_col]
    if sort_col == "gmsc":
        # Game Score is additive, so SUM of the per-game scores == the score of
        # the summed box; scale it into the displayed per-mode rate.
        return _scaled_sort_expr(
            _GMSC_SQL_AGG,
            "COUNT(*)",
            "SUM(minutes_seconds)",
            "SUM(pace * minutes_seconds)",
            mode,
        )
    if sort_col == "min":
        total = "SUM(minutes_seconds)"
        # Minutes display as per-game minutes in every rate mode (* 1.0 forces
        # float division — seconds are integers).
        return total if mode == "totals" else f"{total} * 1.0 / NULLIF(COUNT(*), 0)"
    if sort_col in _COUNTING or sort_col == "plus_minus":
        return _scaled_sort_expr(
            f"SUM({sort_col})",
            "COUNT(*)",
            "SUM(minutes_seconds)",
            "SUM(pace * minutes_seconds)",
            mode,
        )
    # gp (mode-independent) and any other label: sort on the SELECT output alias.
    return sort_col


async def _count_subquery(db: AsyncSession, inner_stmt: Any) -> int:
    """Return the total row count by wrapping ``inner_stmt`` in a COUNT subquery.

    Strategy: SELECT count(*) FROM (<inner_stmt>) AS _count_sq.
    This avoids fetching all rows into Python and works for any SELECT shape.
    """
    count_sq = inner_stmt.subquery("_count_sq")
    count_stmt = select(func.count()).select_from(count_sq)
    result = await db.execute(count_stmt)
    return int(result.scalar() or 0)


def _apply_pagination(stmt: Any, q: ExplorerQuery) -> Any:
    """Apply LIMIT/OFFSET for the requested page, unless the query opts out.

    When ``q.paginate`` is False (CSV export) the statement is returned unsliced
    so every matching row is fetched.
    """
    if not q.paginate:
        return stmt
    return stmt.limit(PAGE_SIZE).offset((q.page - 1) * PAGE_SIZE)


def _player_sort_expr_career(sort_col: str, mode: str) -> Any:
    """Sort expression for career grain sourced from ``summer_league_player_seasons``.

    Uses season-table column aggregates (``SUM(gp)``, ``SUM(minutes)``) instead of
    the game-log aggregates (``COUNT(*)``, ``SUM(minutes_seconds)``) used by the
    old implementation.  ``per_100`` mode divides by the pace-weighted denominator
    ``SUM(pace × minutes) × 60`` (pace is possessions/48); players with no pace in
    scope sort last (NULL), matching the None display value.
    """
    _pct_exprs: dict[str, str] = {
        "efg_pct": "(SUM(fgm) + 0.5 * SUM(fg3m)) / NULLIF(SUM(fga), 0)",
        "fg_pct": "SUM(fgm) * 1.0 / NULLIF(SUM(fga), 0)",
        "fg3_pct": "SUM(fg3m) * 1.0 / NULLIF(SUM(fg3a), 0)",
        "ft_pct": "SUM(ftm) * 1.0 / NULLIF(SUM(fta), 0)",
        "ts_pct": "SUM(pts) / NULLIF(2.0 * (SUM(fga) + 0.44 * SUM(fta)), 0)",
        # Attempt rates recombine from summed box (same ratio the cell displays).
        "fg3ar": "SUM(fg3a) * 1.0 / NULLIF(SUM(fga), 0)",
        "ftr": "SUM(fta) * 1.0 / NULLIF(SUM(fga), 0)",
        # Assisted share of made FGs from summed PBP counts.
        "astd_pct": "SUM(ast_fgm) * 1.0 / NULLIF(SUM(ast_fgm) + SUM(unast_fgm), 0)",
    }
    if sort_col in _pct_exprs:
        return _pct_exprs[sort_col]
    if sort_col == "gmsc":
        # Game Score is additive/linear: SUM of per-competition scores == score of
        # the summed box. Recompute from the summed box and scale into the displayed
        # per-mode rate (matches _compute_player_values' game_score_from_row).
        return _scaled_sort_expr(
            _GMSC_SQL_AGG,
            "SUM(gp)",
            "SUM(minutes) * 60",
            _CAREER_PACE_SEC_SQL,
            mode,
        )
    if sort_col == "min":
        # Minutes display: total in totals mode; per-game rate otherwise.
        return (
            "SUM(minutes)"
            if mode == "totals"
            else "SUM(minutes) * 1.0 / NULLIF(SUM(gp), 0)"
        )
    if sort_col in _COUNTING or sort_col == "plus_minus":
        # sec equivalent = SUM(minutes) * 60; pace_sec = SUM(pace × minutes) × 60.
        return _scaled_sort_expr(
            f"SUM({sort_col})",
            "SUM(gp)",
            "SUM(minutes) * 60",
            _CAREER_PACE_SEC_SQL,
            mode,
        )
    # gp, ws, vorp and the rate composites (per, ortg, drtg, bpm, usg_pct,
    # ast_pct, tov_pct): sort on the labeled SELECT aggregate.
    return sort_col


async def _query_players(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """Aggregate career totals from ``summer_league_player_seasons`` via catalog roll-ups.

    Ticket #405 source switch: reads materialized season rows (one per
    player-competition) rather than raw ``SummerLeaguePlayerGameLog`` records.

    Roll-up semantics per catalog bucket:

    * ``additive``       — ``SUM()`` across competitions (box totals, GP, WS, VORP).
    * ``recombinable``   — recomputed in ``_compute_player_values`` from the summed
                           box components (eFG%, FG%, TS%, etc.) — unchanged.
    * ``rate_composite`` — minute-weighted average in SQL
                           ``SUM(metric × minutes) / SUM(eligible minutes)``
                           where eligible means the metric is non-NULL.

    Box-stat values are lossless versus the prior game-log-sum implementation
    because the season table stores exact box totals accumulated from those logs.

    ``per_100`` mode derives possessions from ``pace`` (possessions/48, NULL
    pre-2017). Possessions observed in pace-covered competitions are extrapolated
    to the player's full minutes via the minute-weighted pace, so full-career
    counting totals aren't divided by only the covered possessions. When coverage
    is partial the rates are approximate — flagged per row via
    ``ExplorerRow.per100_approx``; complete coverage is exact.

    Limitations versus the old game-log query:

    * Round-type filtering is not available at career grain — season rows aggregate
      an entire competition rather than individual rounds.  The ``round_type``
      filter param is silently ignored here; use ``grain=per_game`` to filter by
      round type.
    """
    ps = SummerLeaguePlayerSeason
    pm = PlayerMaster

    conds: list[Any] = []
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
        if q.draft_pick_min is not None:
            conds.append(pm.draft_pick >= q.draft_pick_min)  # type: ignore[operator, arg-type]
        if q.draft_pick_max is not None:
            conds.append(pm.draft_pick <= q.draft_pick_max)  # type: ignore[operator, arg-type]
    if q.country is not None:
        # q.country is a canonical name; match every raw encoding (code/alias)
        # that normalizes to it so filtering is independent of stored form.
        conds.append(pm.birth_country.in_(country_variants(q.country)))  # type: ignore[union-attr]

    def _rate_composite_agg(col: Any) -> Any:
        """``SUM(col × minutes) / NULLIF(SUM(eligible_minutes), 0)``.

        Eligible minutes = minutes from pools where ``col`` is not NULL.
        Mirrors :func:`rollup_rate_composite`: pools with NULL values contribute
        neither to the numerator nor the denominator.
        """
        eligible_min = func.sum(  # type: ignore[attr-defined]
            case((col.isnot(None), ps.minutes), else_=literal(0))  # type: ignore[attr-defined]
        )
        return func.sum(col * ps.minutes) / func.nullif(eligible_min, 0)  # type: ignore[attr-defined]

    # Minutes played in pace-covered competitions (pace is NULL pre-2017).
    paced_min = func.sum(  # type: ignore[attr-defined]
        case((ps.pace.isnot(None), ps.minutes), else_=literal(0))  # type: ignore[union-attr]
    )
    # pace_sec drives per_100 (poss = pace_sec / (60 × 48)). To avoid dividing
    # full-career points by only the pace-covered possessions (which explodes the
    # rate for players whose career straddles the 2017 pace boundary), extrapolate
    # possessions to *all* minutes using the minute-weighted observed pace:
    #   poss_full = poss_covered × (total_min / covered_min).
    # When coverage is complete this factor is 1 (exact); partial coverage is
    # flagged approximate via ExplorerRow.per100_approx. All-NULL pace → NULL → None.
    pace_sec_expr = (
        func.sum(ps.pace * ps.minutes * 60)  # type: ignore[attr-defined,operator]
        * func.sum(ps.minutes)  # type: ignore[attr-defined]
        / func.nullif(paced_min, 0)
    )

    stmt = (
        select(
            pm.slug,
            pm.display_name,
            func.sum(ps.gp).label("gp"),  # type: ignore[attr-defined]
            # _compute_player_values expects seconds; minutes * 60 converts.
            (func.sum(ps.minutes) * 60).label("sec"),  # type: ignore[attr-defined]
            pace_sec_expr.label("pace_sec"),
            paced_min.label("pace_min"),
            func.sum(ps.pts).label("pts"),  # type: ignore[attr-defined]
            func.sum(ps.reb).label("reb"),  # type: ignore[attr-defined]
            func.sum(ps.ast).label("ast"),  # type: ignore[attr-defined]
            func.sum(ps.stl).label("stl"),  # type: ignore[attr-defined]
            func.sum(ps.blk).label("blk"),  # type: ignore[attr-defined]
            func.sum(ps.tov).label("tov"),  # type: ignore[attr-defined]
            func.sum(ps.fgm).label("fgm"),  # type: ignore[attr-defined]
            func.sum(ps.fga).label("fga"),  # type: ignore[attr-defined]
            func.sum(ps.fg3m).label("fg3m"),  # type: ignore[attr-defined]
            func.sum(ps.fg3a).label("fg3a"),  # type: ignore[attr-defined]
            func.sum(ps.ftm).label("ftm"),  # type: ignore[attr-defined]
            func.sum(ps.fta).label("fta"),  # type: ignore[attr-defined]
            func.sum(ps.oreb).label("oreb"),  # type: ignore[attr-defined]
            func.sum(ps.dreb).label("dreb"),  # type: ignore[attr-defined]
            func.sum(ps.pf).label("pf"),  # type: ignore[attr-defined]
            func.sum(ps.plus_minus).label("plus_minus"),  # type: ignore[attr-defined]
            # PBP assisted-FG counts (SQL SUM skips NULLs; all-NULL → NULL).
            func.sum(ps.ast_fgm).label("ast_fgm"),  # type: ignore[attr-defined]
            func.sum(ps.unast_fgm).label("unast_fgm"),  # type: ignore[attr-defined]
            # Additive advanced (exact sum across competitions):
            func.sum(ps.ws).label("ws"),  # type: ignore[attr-defined]
            func.sum(ps.ows).label("ows"),  # type: ignore[attr-defined]
            func.sum(ps.dws).label("dws"),  # type: ignore[attr-defined]
            func.sum(ps.vorp).label("vorp"),  # type: ignore[attr-defined]
            # Rate-composite advanced (minute-weighted average across competitions):
            _rate_composite_agg(ps.per).label("per"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.ortg).label("ortg"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.drtg).label("drtg"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.net_rtg).label("net_rtg"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.obpm).label("obpm"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.dbpm).label("dbpm"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.bpm).label("bpm"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.usg_pct).label("usg_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.ast_pct).label("ast_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.tov_pct).label("tov_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.orb_pct).label("orb_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.drb_pct).label("drb_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.trb_pct).label("trb_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.stl_pct).label("stl_pct"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.blk_pct).label("blk_pct"),  # type: ignore[attr-defined]
            # WS/40's minute-weighted mean equals 40·ΣWS/Σmin — exact, not approx.
            _rate_composite_agg(ps.ws40).label("ws40"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.ws82).label("ws82"),  # type: ignore[attr-defined]
            _rate_composite_agg(ps.vorp82).label("vorp82"),  # type: ignore[attr-defined]
        )  # type: ignore[call-overload, misc]
        .select_from(ps)
        .join(pm, pm.id == ps.player_id)  # type: ignore[arg-type]
        .where(*conds)
        .group_by(ps.player_id, pm.slug, pm.display_name)  # type: ignore[attr-defined]
        .having(func.sum(ps.gp) >= q.min_games)  # type: ignore[attr-defined]
        .having(func.sum(ps.minutes) >= q.min_minutes)  # type: ignore[attr-defined]
    )
    # Age filter (career grain): age = MIN(ps.year) − birth year, anchored to the
    # player's EARLIEST competition in the filtered pool.  ps.year is the integer
    # competition year, equivalent to the old MIN(comp.year) used when joining game
    # logs.  NULL birthdate → NULL age → row excluded (no false match).
    if q.age_min is not None:
        stmt = stmt.having(
            func.min(ps.year)  # type: ignore[attr-defined]
            - func.extract("year", func.min(pm.birthdate))  # type: ignore[arg-type]
            >= q.age_min
        )
    if q.age_max is not None:
        stmt = stmt.having(
            func.min(ps.year)  # type: ignore[attr-defined]
            - func.extract("year", func.min(pm.birthdate))  # type: ignore[arg-type]
            <= q.age_max
        )
    if q.team_slug is not None:
        te = SummerLeagueTeamEntry
        stmt = stmt.join(te, ps.primary_team_entry_id == te.id).where(  # type: ignore[arg-type]
            te.team_slug == q.team_slug
        )
    stmt = _apply_position_filter(stmt, q)
    # Nth-appearance filter: keep only the season rows whose year is the player's
    # Nth distinct SL year, then the GROUP BY aggregates that year (both venues if
    # any) into one career row per qualifying player.
    stmt = _apply_appearance_filter(stmt, q, ps.player_id, ps.year)
    # round_type is not supported at career grain — season rows aggregate a full
    # competition, not individual round games.  Silently ignored here.

    # Apply metric threshold filters as HAVING clauses (career grain).
    # Filters are on SQL aggregate expressions — career totals for counting stats,
    # pooled ratios for percentages, minute-weighted averages for rate composites.
    for mf in q.metric_filters:
        having_expr = _career_metric_having(mf, ps)
        if having_expr is not None:
            stmt = stmt.having(having_expr)  # type: ignore[arg-type]

    # Count total matching rows before slicing (wrapping subquery avoids fetching all rows).
    total = await _count_subquery(db, stmt)

    # Apply SQL ORDER BY (NULLS LAST) + LIMIT + OFFSET.
    sort_expr = _player_sort_expr_career(q.sort, q.mode)
    direction = "DESC" if q.direction == "desc" else "ASC"
    stmt = stmt.order_by(nulls_last(text(f"{sort_expr} {direction}")))
    stmt = _apply_pagination(stmt, q)

    raw = list((await db.execute(stmt)).all())

    def _r1(v: Any) -> Optional[float]:
        return round(float(v), 1) if v is not None else None

    def _per100_approx(r: Any) -> bool:
        """True when per_100 possessions are extrapolated over part of the career.

        Approximate iff some minutes have pace and some do not: possessions for the
        pace-missing minutes are estimated from the observed pace. All-covered →
        exact; none-covered → cells are None (nothing to approximate/estimate).
        """
        if q.mode != "per_100":
            return False
        total_min = float(r.sec or 0) / 60.0
        pace_min = float(getattr(r, "pace_min", 0) or 0)
        return 0.0 < pace_min < total_min

    rows = [
        ExplorerRow(
            label=r.display_name or "Player",
            href=f"/players/{r.slug}" if r.slug else None,
            values={
                **_compute_player_values(r, q.mode),
                # Advanced roll-up values (ticket #405).
                # Additive: exact sum across all competitions in scope.
                "ws": _r1(r.ws),
                "ows": _r1(r.ows),
                "dws": _r1(r.dws),
                "vorp": _r1(r.vorp),
                # Rate composite: minute-weighted average (approximate; labelled "#406").
                "per": _r1(r.per),
                "ortg": _r1(r.ortg),
                "drtg": _r1(r.drtg),
                "net_rtg": _r1(r.net_rtg),
                "obpm": _r1(r.obpm),
                "dbpm": _r1(r.dbpm),
                "bpm": _r1(r.bpm),
                "usg_pct": _r1(r.usg_pct),
                "ast_pct": _r1(r.ast_pct),
                "tov_pct": _r1(r.tov_pct),
                "orb_pct": _r1(r.orb_pct),
                "drb_pct": _r1(r.drb_pct),
                "trb_pct": _r1(r.trb_pct),
                "stl_pct": _r1(r.stl_pct),
                "blk_pct": _r1(r.blk_pct),
                # WS/40 lives in the hundredths — keep 2 decimals.
                "ws40": round(float(r.ws40), 2) if r.ws40 is not None else None,
                "ws82": _r1(r.ws82),
                "vorp82": _r1(r.vorp82),
            },
            per100_approx=_per100_approx(r),
        )
        for r in raw
    ]

    elig_n, elig_m = await _fetch_adv_counts(db, q)
    return ExplorerResult(
        subject="players",
        available=True,
        columns=_PLAYER_STAT_COLUMNS + _PLAYER_ADVANCED_COLUMNS,
        rows=rows,
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=(q.page - 1) * PAGE_SIZE + len(rows) < total,
        facets=ExplorerFacets(),
        query=q,
        pooled_composite_keys=frozenset(
            c.key
            for c in _PLAYER_ADVANCED_COLUMNS
            if c.bucket == _BUCKET_RATE_COMPOSITE
        ),
        adv_eligible_n=elig_n,
        adv_eligible_m=elig_m,
    )


def _is_single_competition(q: ExplorerQuery) -> bool:
    """Return True when the query pins exactly one competition (one year, one venue).

    Both year_min == year_max (pinned to a single year) and a non-None venue are
    required.  This is the only case where pool-calibrated composites are valid.
    """
    return (
        q.year_min is not None
        and q.year_max is not None
        and q.year_min == q.year_max
        and q.venue is not None
    )


async def _fetch_adv_eligible(db: AsyncSession, year: int, venue_slug: str) -> bool:
    """Look up the adv_eligible flag for a single (year, venue_slug) competition pool.

    Returns False when no matching metric context row exists (pool not yet computed).
    """
    ctx = SummerLeagueMetricContext
    result = await db.execute(
        select(ctx.adv_eligible)  # type: ignore[call-overload]
        .where(ctx.year == year)  # type: ignore[arg-type]
        .where(ctx.venue_slug == venue_slug)  # type: ignore[arg-type]
        .limit(1)
    )
    row = result.one_or_none()
    return bool(row[0]) if row is not None else False


async def _fetch_adv_counts(db: AsyncSession, q: ExplorerQuery) -> tuple[int, int]:
    """Count eligible and total competitions in the query scope for the N-of-M banner.

    Queries ``SummerLeagueMetricContext`` with the same year/venue filters as the
    main player query.  Returns ``(eligible_n, total_m)`` where ``eligible_n`` is
    the count with ``adv_eligible=True`` and ``total_m`` is the total count of rows
    with a metric context (competitions without a context row are not counted).

    Args:
        db: Async database session.
        q:  Parsed explorer query providing year/venue filter bounds.

    Returns:
        Tuple ``(eligible_n, total_m)``.
    """
    ctx = SummerLeagueMetricContext
    conds: list[Any] = []
    if q.year_min is not None:
        conds.append(ctx.year >= q.year_min)  # type: ignore[arg-type]
    if q.year_max is not None:
        conds.append(ctx.year <= q.year_max)  # type: ignore[arg-type]
    if q.venue:
        conds.append(ctx.venue_slug == q.venue)

    # Single grouped query: total rows + eligible rows via a conditional sum. Folding
    # these into one statement keeps the route's query budget tight (one count query,
    # not two) versus issuing separate total/eligible COUNT(*) statements.
    eligible_expr = func.coalesce(
        func.sum(case((ctx.adv_eligible.is_(True), 1), else_=literal(0))),  # type: ignore[attr-defined]
        0,
    )
    stmt = select(func.count(), eligible_expr).select_from(ctx)
    if conds:
        stmt = stmt.where(*conds)
    row = (await db.execute(stmt)).one()
    total_m = int(row[0] or 0)
    eligible_n = int(row[1] or 0)

    return eligible_n, total_m


async def _query_players_per_competition(
    db: AsyncSession, q: ExplorerQuery
) -> ExplorerResult:
    """One row per (player, competition): season box totals from SummerLeaguePlayerSeason.

    Sort and pagination happen in SQL (ORDER BY + LIMIT + OFFSET).  Count uses
    a wrapping COUNT(*) subquery on the unsliced statement.

    When the query pins a single competition (one year + one venue), composite
    columns (PER, ORtg, DRtg, BPM, WS, VORP) from ``summer_league_player_seasons``
    are appended to the result.  ``adv_eligible`` on the result tells the template
    whether those composites are valid for this pool; when False a warning banner
    is shown and the composite columns are omitted from the column list.
    """
    ps = SummerLeaguePlayerSeason
    comp = SummerLeagueCompetition
    pm = PlayerMaster

    # Detect single-competition scope: composites are only valid within one pool.
    single_comp = _is_single_competition(q)

    # Alias minutes*60 as sec so _compute_player_values works unchanged.
    # pace_sec = pace * minutes * 60 (pace is possessions/48): each row is one
    # competition, so per_100 is exact where pace is present and NULL otherwise.
    conds: list[Any] = [
        ps.gp >= q.min_games,  # type: ignore[operator]
        ps.minutes >= q.min_minutes,  # minutes stored as minutes, not seconds
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
        if q.draft_pick_min is not None:
            conds.append(pm.draft_pick >= q.draft_pick_min)  # type: ignore[operator, arg-type]
        if q.draft_pick_max is not None:
            conds.append(pm.draft_pick <= q.draft_pick_max)  # type: ignore[operator, arg-type]
    if q.country is not None:
        # q.country is a canonical name; match every raw encoding (code/alias)
        # that normalizes to it so filtering is independent of stored form.
        conds.append(pm.birth_country.in_(country_variants(q.country)))  # type: ignore[union-attr]
    # Age filter (per_competition grain): age = competition year − birth year.
    # One row per (player, competition), so the year is the competition year directly
    # (ps.year).  NULL birth dates naturally produce NULL age and are excluded (no match).
    if q.age_min is not None:
        conds.append(
            ps.year - func.extract("year", pm.birthdate)  # type: ignore[arg-type]
            >= q.age_min
        )
    if q.age_max is not None:
        conds.append(
            ps.year - func.extract("year", pm.birthdate)  # type: ignore[arg-type]
            <= q.age_max
        )
    # Metric threshold filters applied as WHERE conditions on season column values.
    # Each row is one competition's season totals; filters are on raw column values,
    # not mode-scaled display rates.
    for mf in q.metric_filters:
        where_expr = _per_comp_metric_where(mf, ps)
        if where_expr is not None:
            conds.append(where_expr)

    base_select = [
        pm.slug,
        pm.display_name,
        ps.year,
        ps.venue_slug,
        ps.gp.label("gp"),  # type: ignore[attr-defined]
        (ps.minutes * 60).label("sec"),  # type: ignore[attr-defined]
        (ps.pace * ps.minutes * 60).label("pace_sec"),  # type: ignore[attr-defined,operator]
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
        ps.ast_fgm.label("ast_fgm"),  # type: ignore[attr-defined, union-attr]
        ps.unast_fgm.label("unast_fgm"),  # type: ignore[attr-defined, union-attr]
    ]

    # Always SELECT composite columns so they are available at all per_competition
    # scopes (single-comp and multi-comp).  The column list and template control
    # whether they are displayed; values are NULL when a pool is not eligible.
    base_select += [
        ps.per.label("per"),  # type: ignore[attr-defined, union-attr]
        ps.ortg.label("ortg"),  # type: ignore[attr-defined, union-attr]
        ps.drtg.label("drtg"),  # type: ignore[attr-defined, union-attr]
        ps.net_rtg.label("net_rtg"),  # type: ignore[attr-defined, union-attr]
        ps.obpm.label("obpm"),  # type: ignore[attr-defined, union-attr]
        ps.dbpm.label("dbpm"),  # type: ignore[attr-defined, union-attr]
        ps.bpm.label("bpm"),  # type: ignore[attr-defined, union-attr]
        ps.ws.label("ws"),  # type: ignore[attr-defined, union-attr]
        ps.ows.label("ows"),  # type: ignore[attr-defined, union-attr]
        ps.dws.label("dws"),  # type: ignore[attr-defined, union-attr]
        ps.ws40.label("ws40"),  # type: ignore[attr-defined, union-attr]
        ps.ws82.label("ws82"),  # type: ignore[attr-defined, union-attr]
        ps.vorp.label("vorp"),  # type: ignore[attr-defined, union-attr]
        ps.vorp82.label("vorp82"),  # type: ignore[attr-defined, union-attr]
        ps.usg_pct.label("usg_pct"),  # type: ignore[attr-defined, union-attr]
        ps.ast_pct.label("ast_pct"),  # type: ignore[attr-defined, union-attr]
        ps.tov_pct.label("tov_pct"),  # type: ignore[attr-defined, union-attr]
        ps.orb_pct.label("orb_pct"),  # type: ignore[attr-defined, union-attr]
        ps.drb_pct.label("drb_pct"),  # type: ignore[attr-defined, union-attr]
        ps.trb_pct.label("trb_pct"),  # type: ignore[attr-defined, union-attr]
        ps.stl_pct.label("stl_pct"),  # type: ignore[attr-defined, union-attr]
        ps.blk_pct.label("blk_pct"),  # type: ignore[attr-defined, union-attr]
    ]

    stmt = (
        select(*base_select)  # type: ignore[call-overload, misc]
        .select_from(ps)
        .join(comp, comp.id == ps.competition_id)  # type: ignore[arg-type]
        .join(pm, pm.id == ps.player_id)  # type: ignore[arg-type]
        .where(*conds)
    )
    if q.team_slug is not None:
        te = SummerLeagueTeamEntry
        stmt = stmt.join(
            te,
            ps.primary_team_entry_id == te.id,  # type: ignore[arg-type]
        ).where(te.team_slug == q.team_slug)  # type: ignore[arg-type]
    stmt = _apply_position_filter(stmt, q)
    # Keep only each player's Nth-appearance competition(s); one map row per
    # player-year, so a same-year two-venue return surfaces both competition rows.
    stmt = _apply_appearance_filter(stmt, q, ps.player_id, ps.year)
    if q.player_slug is not None:
        stmt = stmt.where(pm.slug == q.player_slug)  # type: ignore[arg-type]

    # Count via wrapping subquery, then slice.
    total = await _count_subquery(db, stmt)

    # per_competition rows are one-per-(player, competition) season totals, so
    # the sort references raw column labels (no aggregation).  Counting stats sort
    # on the displayed per-mode rate — matching _compute_player_values — so the
    # column stays visually monotonic; percentages use NULLIF-guarded ratios.
    # ``minutes`` is stored in minutes; sec = minutes * 60, pace_sec = pace *
    # minutes * 60 (pace is possessions/48); rows without pace sort last in per_100.
    _pc_pct_exprs: dict[str, str] = {
        "efg_pct": "(fgm + 0.5 * fg3m) / NULLIF(fga, 0)",
        "fg_pct": "fgm * 1.0 / NULLIF(fga, 0)",
        "fg3_pct": "fg3m * 1.0 / NULLIF(fg3a, 0)",
        "ft_pct": "ftm * 1.0 / NULLIF(fta, 0)",
        "ts_pct": "pts / NULLIF(2.0 * (fga + 0.44 * fta), 0)",
        # Attempt rates recombine from the row's box (matches the displayed ratio).
        "fg3ar": "fg3a * 1.0 / NULLIF(fga, 0)",
        "ftr": "fta * 1.0 / NULLIF(fga, 0)",
        # Assisted share of made FGs from the row's PBP counts.
        "astd_pct": "ast_fgm * 1.0 / NULLIF(ast_fgm + unast_fgm, 0)",
    }
    if q.sort in _pc_pct_exprs:
        sort_expr: str = _pc_pct_exprs[q.sort]
    elif q.sort == "gmsc":
        sort_expr = _scaled_sort_expr(
            _GMSC_SQL_RAW, "gp", "minutes * 60", "pace * minutes * 60", q.mode
        )
    elif q.sort == "min":
        sort_expr = "minutes" if q.mode == "totals" else "minutes / NULLIF(gp, 0)"
    elif q.sort in _COUNTING or q.sort == "plus_minus":
        sort_expr = _scaled_sort_expr(
            q.sort, "gp", "minutes * 60", "pace * minutes * 60", q.mode
        )
    else:
        # gp and the advanced composites (per/ortg/bpm/…) sort on the raw label.
        sort_expr = q.sort
    direction = "DESC" if q.direction == "desc" else "ASC"
    stmt = stmt.order_by(nulls_last(text(f"{sort_expr} {direction}")))
    stmt = _apply_pagination(stmt, q)

    raw = list((await db.execute(stmt)).all())

    # Determine adv_eligible for single-competition scope.
    pool_adv_eligible = False
    if single_comp and q.year_min is not None and q.venue is not None:
        pool_adv_eligible = await _fetch_adv_eligible(db, q.year_min, q.venue)

    # N-of-M counts for the eligibility banner.
    elig_n, elig_m = await _fetch_adv_counts(db, q)

    # Build result column list.
    # - Single-comp + eligible: advanced columns shown (exact within one pool, no caveat).
    # - Single-comp + not eligible: base columns only (warning banner shown by template).
    # - Multi-comp: advanced columns always shown (N-of-M banner; each row is one pool,
    #   so per-row values are exact within that pool — no "avg" marker needed here).
    if single_comp and not pool_adv_eligible:
        columns: list[ExplorerColumn] = list(_PLAYER_STAT_COLUMNS)
    else:
        columns = list(_PLAYER_STAT_COLUMNS) + list(_PLAYER_ADVANCED_COLUMNS)

    _ADV_ROW_KEYS = (
        "per",
        "ortg",
        "drtg",
        "net_rtg",
        "obpm",
        "dbpm",
        "bpm",
        "ws",
        "ows",
        "dws",
        "ws40",
        "ws82",
        "vorp",
        "vorp82",
        "usg_pct",
        "ast_pct",
        "tov_pct",
        "orb_pct",
        "drb_pct",
        "trb_pct",
        "stl_pct",
        "blk_pct",
    )

    def _adv_values(r: Any) -> dict[str, Any]:
        """Extract composite values from a result row (always present in SELECT now)."""
        return {k: getattr(r, k, None) for k in _ADV_ROW_KEYS}

    rows = [
        ExplorerRow(
            label=f"{r.display_name} · {_venue_label(r.venue_slug)} {r.year}",
            href=f"/players/{r.slug}" if r.slug else None,
            values={
                **_compute_player_values(r, q.mode),
                **_adv_values(r),
            },
        )
        for r in raw
    ]

    return ExplorerResult(
        subject="players",
        available=True,
        columns=columns,
        rows=rows,
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=(q.page - 1) * PAGE_SIZE + len(rows) < total,
        facets=ExplorerFacets(),
        query=q,
        adv_eligible=pool_adv_eligible if single_comp else False,
        adv_eligible_n=elig_n,
        adv_eligible_m=elig_m,
    )


async def _query_players_per_game(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """One row per player game log (no aggregation).

    This grain has the highest row count, making SQL-level pagination most
    critical.  Sort column maps directly to a raw column label in the SELECT;
    percentage stats use NULLIF-guarded expressions on raw column labels.
    Count uses a wrapping COUNT(*) subquery.
    """
    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition
    pm = PlayerMaster
    game = SummerLeagueGame
    player_team = aliased(SummerLeagueTeamEntry)
    home_team = aliased(SummerLeagueTeamEntry)
    away_team = aliased(SummerLeagueTeamEntry)

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
        if q.draft_pick_min is not None:
            conds.append(pm.draft_pick >= q.draft_pick_min)  # type: ignore[operator, arg-type]
        if q.draft_pick_max is not None:
            conds.append(pm.draft_pick <= q.draft_pick_max)  # type: ignore[operator, arg-type]
    if q.country is not None:
        # q.country is a canonical name; match every raw encoding (code/alias)
        # that normalizes to it so filtering is independent of stored form.
        conds.append(pm.birth_country.in_(country_variants(q.country)))  # type: ignore[union-attr]
    if q.round_type is not None:
        conds.append(game.round_label == q.round_type)  # type: ignore[arg-type]
    # Age at the time of the game = competition year - birth year. Applied as a
    # per-row WHERE (per_game is not aggregated). NULL birthdate yields NULL → the
    # row is excluded, matching the career/per_competition grains. Without this the
    # Age range control silently no-ops for grain=per_game.
    if q.age_min is not None:
        conds.append(
            comp.year - func.extract("year", pm.birthdate) >= q.age_min  # type: ignore[arg-type]
        )
    if q.age_max is not None:
        conds.append(
            comp.year - func.extract("year", pm.birthdate) <= q.age_max  # type: ignore[arg-type]
        )
    # Metric threshold filters applied as WHERE conditions on game-log columns.
    # Advanced composites (per/ortg/bpm/ws/vorp) are not stored on game logs and are
    # silently skipped by _per_game_metric_where.
    for mf in q.metric_filters:
        where_expr = _per_game_metric_where(mf, pgl)
        if where_expr is not None:
            conds.append(where_expr)

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
            player_team.raw_team_abbreviation.label("team_abbr"),  # type: ignore[union-attr]
            player_team.raw_team_name.label("team_name"),  # type: ignore[union-attr, attr-defined]
            case(
                (
                    pgl.team_entry_id == game.home_team_entry_id,
                    away_team.raw_team_abbreviation,
                ),  # type: ignore[arg-type]
                else_=home_team.raw_team_abbreviation,
            ).label("opponent_abbr"),  # type: ignore[arg-type, attr-defined]
            case(
                (
                    pgl.team_entry_id == game.home_team_entry_id,
                    away_team.raw_team_name,
                ),  # type: ignore[arg-type]
                else_=home_team.raw_team_name,
            ).label("opponent_name"),  # type: ignore[arg-type, attr-defined]
            # pace_sec: 0 for single-game rows (per_100 mode will show None)
            literal(0).label("pace_sec"),
        )  # type: ignore[call-overload, misc]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(pm, pm.id == pgl.player_id)
        .join(game, game.id == pgl.game_id)
        .join(player_team, player_team.id == pgl.team_entry_id, isouter=True)
        .join(home_team, home_team.id == game.home_team_entry_id, isouter=True)
        .join(away_team, away_team.id == game.away_team_entry_id, isouter=True)
        .where(*conds)
    )
    if q.team_slug is not None:
        stmt = stmt.where(player_team.team_slug == q.team_slug)  # type: ignore[arg-type]
    stmt = _apply_position_filter(stmt, q)
    # Keep only game logs from each player's Nth-appearance year (rank derived from
    # the season table; the competition year is the join key here).
    stmt = _apply_appearance_filter(stmt, q, pgl.player_id, comp.year)

    # Count via wrapping subquery, then sort + slice.
    total = await _count_subquery(db, stmt)

    # Percentage sort expressions operate on raw per-game-log column labels.
    _pg_pct_exprs: dict[str, str] = {
        "efg_pct": "(fgm + 0.5 * fg3m) / NULLIF(fga, 0)",
        "fg_pct": "fgm * 1.0 / NULLIF(fga, 0)",
        "fg3_pct": "fg3m * 1.0 / NULLIF(fg3a, 0)",
        "ft_pct": "ftm * 1.0 / NULLIF(fta, 0)",
        "ts_pct": "pts / NULLIF(2.0 * (fga + 0.44 * fta), 0)",
        "min": "sec",
        "gp": "1",  # every row is 1 game; stable but well-defined
        # Single game: GmSc is the raw box score (gp=1), matching the displayed cell.
        "gmsc": _GMSC_SQL_RAW,
    }
    sort_expr = _pg_pct_exprs.get(q.sort, q.sort)
    direction = "DESC" if q.direction == "desc" else "ASC"
    stmt = stmt.order_by(nulls_last(text(f"{sort_expr} {direction}")))
    stmt = _apply_pagination(stmt, q)

    raw = list((await db.execute(stmt)).all())

    rows = []
    for r in raw:
        date_str = r.game_date.isoformat() if r.game_date else "—"
        team_label = r.team_abbr or r.team_name or "Team"
        opponent_label = r.opponent_abbr or r.opponent_name or "Opponent"
        # Build a namespace with gp=1 so _compute_player_values treats each row as one game.
        row_ns = _SingleGameRow(r)
        rows.append(
            ExplorerRow(
                label=f"{r.display_name} · {team_label} vs {opponent_label} · {date_str}",
                href=f"/stats/summer-league/{r.year}/games/{r.game_id}",
                values=_compute_player_values(row_ns, q.mode),
            )
        )

    return ExplorerResult(
        subject="players",
        available=True,
        columns=_PLAYER_STAT_COLUMNS,
        rows=rows,
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=(q.page - 1) * PAGE_SIZE + len(rows) < total,
        facets=ExplorerFacets(),
        query=q,
    )


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
    """One row per team-season: record + scoring from games, ratings from box logs.

    The raw per-game averages (ppg, diff, pace, etc.) are computed in Python
    after fetching the full (filtered) team set, because the teams subject has
    at most hundreds of rows and requires a complex multi-source assembly from
    games + team-box-logs.  Pagination is still SQL-free on the assembled rows,
    but `_paginate()` is gone — we inline the sort + slice here.
    """
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
    # Apply the same round_type scope as game-score records so the row is consistent.
    tgl = SummerLeagueTeamGameLog
    rating_stmt = (
        select(
            tgl.team_entry_id,
            func.avg(tgl.pace).label("pace"),
            func.avg(tgl.off_rating).label("ortg"),
            func.avg(tgl.def_rating).label("drtg"),
        )  # type: ignore[call-overload, misc]
        .where(tgl.team_entry_id.in_(entry_ids))  # type: ignore[attr-defined]
        .group_by(tgl.team_entry_id)
    )
    if q.round_type is not None:
        rating_stmt = rating_stmt.join(game, tgl.game_id == game.id).where(
            game.round_label == q.round_type
        )
    rating_rows = (await db.execute(rating_stmt)).all()
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
        # Derive box-log averages; emit None when the log is absent or all-NULL
        # so missing inputs degrade cleanly instead of producing 0 / NaN.
        ortg_val = _r1(rt.ortg) if rt else None
        drtg_val = _r1(rt.drtg) if rt else None
        net_rtg_val = (
            round(ortg_val - drtg_val, 1)
            if (ortg_val is not None and drtg_val is not None)
            else None
        )
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
                    "ortg": ortg_val,
                    "drtg": drtg_val,
                    "net_rtg": net_rtg_val,
                },
            )
        )

    # Teams row count is bounded (hundreds at most), so sort + slice in Python.
    # This avoids the complexity of a multi-CTE SQL approach while still removing
    # the shared `_paginate()` helper.
    return _build_result("teams", _TEAM_STAT_COLUMNS, rows, q)


# --------------------------------------------------------------------------- #
# Games subject
# --------------------------------------------------------------------------- #


async def _query_games(db: AsyncSession, q: ExplorerQuery) -> ExplorerResult:
    """One row per game: matchup/score in the label, total + margin sortable in SQL."""
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

    # Include computed sort columns (total, margin) directly in the SELECT so
    # ORDER BY can reference them by label without repeating the expression.
    inner_stmt = (
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
            (game.home_score + game.away_score).label("total"),  # type: ignore[operator, union-attr]
            func.abs(game.home_score - game.away_score).label("margin"),  # type: ignore[operator]
        )  # type: ignore[call-overload, misc]
        .select_from(game)
        .join(comp, comp.id == game.competition_id)
        .join(home, home.id == game.home_team_entry_id, isouter=True)
        .join(away, away.id == game.away_team_entry_id, isouter=True)
        .where(*conds)
    )

    # Count via wrapping subquery.
    total = await _count_subquery(db, inner_stmt)

    # Sort on the computed label (total or margin); both are valid column labels.
    sort_col = q.sort if q.sort in ("total", "margin") else "total"
    direction = "DESC" if q.direction == "desc" else "ASC"
    stmt = inner_stmt.order_by(nulls_last(text(f"{sort_col} {direction}")))
    stmt = _apply_pagination(stmt, q)

    game_rows = (await db.execute(stmt)).all()

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
                    "total": r.total,
                    "margin": r.margin,
                },
            )
        )

    return ExplorerResult(
        subject="games",
        available=True,
        columns=_GAME_STAT_COLUMNS,
        rows=rows,
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=(q.page - 1) * PAGE_SIZE + len(rows) < total,
        facets=ExplorerFacets(),
        query=q,
    )


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _build_result(
    subject: str,
    columns: list[ExplorerColumn],
    rows: list[ExplorerRow],
    q: ExplorerQuery,
) -> ExplorerResult:
    """Sort rows by the query's column (nulls last), then slice to the page.

    Used only for the teams subject where the rows are assembled from multiple
    Python-side queries.  Players and games paginate entirely in SQL.
    """
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
    if q.paginate:
        start = (q.page - 1) * PAGE_SIZE
        page_rows = rows[start : start + PAGE_SIZE]
        has_next = start + PAGE_SIZE < total
    else:
        page_rows = rows
        has_next = False
    return ExplorerResult(
        subject=subject,
        available=True,
        columns=columns,
        rows=page_rows,
        total=total,
        page=q.page,
        page_size=PAGE_SIZE,
        has_next=has_next,
        facets=ExplorerFacets(),  # filled by the caller
        query=q,
    )


def _empty_result(subject: str, q: ExplorerQuery) -> ExplorerResult:
    """An available result with no rows (e.g. filters matched nothing)."""
    return _build_result(subject, _COLUMNS_BY_SUBJECT.get(subject, []), [], q)


# --------------------------------------------------------------------------- #
# Drill-down
# --------------------------------------------------------------------------- #


async def get_player_drilldown_rows(
    db: AsyncSession,
    player_slug: str,
    scope_q: ExplorerQuery,
) -> ExplorerResult:
    """Per-competition breakdown for a single player (drill-down from career grain).

    Fetches all per-competition season rows for the player within the same scope
    (year/venue) as the career query, with minimum-game/minute floors relaxed to
    ``1`` so sparse early-career competitions are included.  Pagination is
    disabled — a player typically appears in at most a handful of competitions.

    Advanced columns are always shown when available per pool; composite values
    are exact within each pool (no "avg" marker) since each row is one competition.

    Args:
        db:        Async database session.
        player_slug: Slug of the player to expand.
        scope_q:   The active career-grain query providing year/venue scope.

    Returns:
        :class:`ExplorerResult` with per-competition rows for the player.
    """
    # Coerce sort away from advanced-composite keys not available in per_game
    # (we stay in per_competition where they are available, but reuse the guard).
    safe_sort = scope_q.sort if scope_q.sort not in _ADV_COMPOSITE_SORT_KEYS else "pts"

    drill_q = ExplorerQuery(
        subject="players",
        grain="per_competition",
        year_min=scope_q.year_min,
        year_max=scope_q.year_max,
        venue=scope_q.venue,
        # Carry the team scope so the breakdown matches the (team-scoped) parent
        # career row — without it, a player on multiple SL teams would show
        # competitions that did not contribute to the displayed total.
        team_slug=scope_q.team_slug,
        # Carry the appearance scope too, so an expanded row shows only the
        # competition(s) that fed the (appearance-filtered) parent career total.
        appearance=scope_q.appearance,
        min_games=1,  # no GP floor — show all competitions for this player
        min_minutes=1,  # no MIN floor
        mode=scope_q.mode,
        sort=safe_sort,
        direction=scope_q.direction,
        page=1,
        paginate=False,  # always return all rows; drilldowns are always small
        player_slug=player_slug,
    )
    return await _query_players_per_competition(db, drill_q)


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

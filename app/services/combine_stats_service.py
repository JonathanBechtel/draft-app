"""Public stats service for combine leaderboards and metric exploration.

Provides leaderboard queries, metric metadata, and summary card data
for the /stats section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Float

from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting, SHOOTING_DRILL_COLUMNS
from app.schemas.players_master import PlayerMaster
from app.schemas.player_status import PlayerStatus
from app.schemas.positions import Position
from app.schemas.seasons import Season
from app.utils.combine_formatters import (
    format_agility_value,
    format_anthro_value,
)


# === Metric Column Mapping ===


class MetricColumnDef(NamedTuple):
    """Definition for a single combine metric."""

    table: type
    column: str
    display_name: str
    unit: str | None
    category: str  # "measurements", "athletic_testing"
    sort_direction: str  # "asc" or "desc"


METRIC_COLUMN_MAP: dict[str, MetricColumnDef] = {
    # Anthropometrics (measurements)
    "wingspan_in": MetricColumnDef(
        CombineAnthro, "wingspan_in", "Wingspan", "in", "measurements", "desc"
    ),
    "standing_reach_in": MetricColumnDef(
        CombineAnthro,
        "standing_reach_in",
        "Standing Reach",
        "in",
        "measurements",
        "desc",
    ),
    "height_w_shoes_in": MetricColumnDef(
        CombineAnthro,
        "height_w_shoes_in",
        "Height (w/ Shoes)",
        "in",
        "measurements",
        "desc",
    ),
    "height_wo_shoes_in": MetricColumnDef(
        CombineAnthro,
        "height_wo_shoes_in",
        "Height (Barefoot)",
        "in",
        "measurements",
        "desc",
    ),
    "weight_lb": MetricColumnDef(
        CombineAnthro, "weight_lb", "Weight", "lbs", "measurements", "desc"
    ),
    "body_fat_pct": MetricColumnDef(
        CombineAnthro,
        "body_fat_pct",
        "Body Fat %",
        "%",
        "measurements",
        "asc",
    ),
    "hand_length_in": MetricColumnDef(
        CombineAnthro,
        "hand_length_in",
        "Hand Length",
        "in",
        "measurements",
        "desc",
    ),
    "hand_width_in": MetricColumnDef(
        CombineAnthro,
        "hand_width_in",
        "Hand Width",
        "in",
        "measurements",
        "desc",
    ),
    # Agility / Athletic Testing
    "lane_agility_time_s": MetricColumnDef(
        CombineAgility,
        "lane_agility_time_s",
        "Lane Agility",
        "s",
        "athletic_testing",
        "asc",
    ),
    "shuttle_run_s": MetricColumnDef(
        CombineAgility,
        "shuttle_run_s",
        "Shuttle Run",
        "s",
        "athletic_testing",
        "asc",
    ),
    "three_quarter_sprint_s": MetricColumnDef(
        CombineAgility,
        "three_quarter_sprint_s",
        "3/4 Court Sprint",
        "s",
        "athletic_testing",
        "asc",
    ),
    "standing_vertical_in": MetricColumnDef(
        CombineAgility,
        "standing_vertical_in",
        "Standing Vertical",
        "in",
        "athletic_testing",
        "desc",
    ),
    "max_vertical_in": MetricColumnDef(
        CombineAgility,
        "max_vertical_in",
        "Max Vertical",
        "in",
        "athletic_testing",
        "desc",
    ),
    "bench_press_reps": MetricColumnDef(
        CombineAgility,
        "bench_press_reps",
        "Bench Press",
        "reps",
        "athletic_testing",
        "desc",
    ),
    # Shooting Drills
    "off_dribble_pct": MetricColumnDef(
        CombineShooting,
        "off_dribble_pct",
        "Off the Dribble",
        "%",
        "shooting",
        "desc",
    ),
    "spot_up_pct": MetricColumnDef(
        CombineShooting, "spot_up_pct", "Spot Up", "%", "shooting", "desc"
    ),
    "three_point_star_pct": MetricColumnDef(
        CombineShooting,
        "three_point_star_pct",
        "3-Point Star",
        "%",
        "shooting",
        "desc",
    ),
    "midrange_star_pct": MetricColumnDef(
        CombineShooting,
        "midrange_star_pct",
        "Mid-Range Star",
        "%",
        "shooting",
        "desc",
    ),
    "three_point_side_pct": MetricColumnDef(
        CombineShooting,
        "three_point_side_pct",
        "3-Point Side",
        "%",
        "shooting",
        "desc",
    ),
    "midrange_side_pct": MetricColumnDef(
        CombineShooting,
        "midrange_side_pct",
        "Mid-Range Side",
        "%",
        "shooting",
        "desc",
    ),
    "free_throw_pct": MetricColumnDef(
        CombineShooting, "free_throw_pct", "Free Throw", "%", "shooting", "desc"
    ),
}


# === Dataclasses ===


@dataclass
class MetricInfo:
    """Metadata for a single combine metric."""

    key: str
    display_name: str
    unit: str | None
    category: str
    sort_direction: str


@dataclass
class LeaderboardEntry:
    """Single row in a leaderboard."""

    rank: int
    player_id: int
    display_name: str
    slug: str
    school: str | None
    position: str | None
    draft_year: int | None
    draft_round: int | None
    draft_pick: int | None
    is_active_nba: bool
    raw_value: float
    formatted_value: str
    percentile: float | None


@dataclass
class LeaderboardResult:
    """Full leaderboard response including summary card data."""

    entries: list[LeaderboardEntry]
    total: int
    metric: MetricInfo
    best: LeaderboardEntry | None
    worst: LeaderboardEntry | None
    median_value: float | None
    typical: LeaderboardEntry | None


# === Public API: Static Lookups ===


def get_all_metrics() -> list[MetricInfo]:
    """Return all available combine metrics."""
    return [
        MetricInfo(
            key=key,
            display_name=d.display_name,
            unit=d.unit,
            category=d.category,
            sort_direction=d.sort_direction,
        )
        for key, d in METRIC_COLUMN_MAP.items()
    ]


def get_metric_info(key: str) -> MetricInfo | None:
    """Look up a single metric by key."""
    d = METRIC_COLUMN_MAP.get(key)
    if d is None:
        return None
    return MetricInfo(
        key=key,
        display_name=d.display_name,
        unit=d.unit,
        category=d.category,
        sort_direction=d.sort_direction,
    )


def get_metrics_grouped() -> dict[str, list[MetricInfo]]:
    """Return metrics grouped by category for dropdown optgroups."""
    groups: dict[str, list[MetricInfo]] = {}
    for key, d in METRIC_COLUMN_MAP.items():
        info = MetricInfo(
            key=key,
            display_name=d.display_name,
            unit=d.unit,
            category=d.category,
            sort_direction=d.sort_direction,
        )
        groups.setdefault(d.category, []).append(info)
    return groups


# === Public API: Database Queries ===


def _format_value(metric_key: str, value: Any) -> str:
    """Format a raw metric value for display."""
    defn = METRIC_COLUMN_MAP[metric_key]
    if defn.table is CombineAnthro:
        result = format_anthro_value(defn.column, value)
    elif defn.table is CombineAgility:
        result = format_agility_value(defn.column, value)
    elif defn.table is CombineShooting:
        result = f"{value:.1f}%" if value is not None else None
    else:
        result = str(value) if value is not None else None
    return result or str(value)


def _build_base_query(
    metric_key: str,
    *,
    years: list[int] | None = None,
    positions: list[str] | None = None,
    is_active_nba: bool | None = None,
) -> Any:
    """Build the base SELECT query for a metric with filters.

    Returns a select() statement that yields:
        (combine_table, PlayerMaster, PlayerStatus, Position, Season)
    Position is joined via the combine table's position_id (the position
    recorded at the combine), not PlayerStatus (current NBA position).
    """
    defn = METRIC_COLUMN_MAP[metric_key]
    table = defn.table
    col = getattr(table, defn.column)

    stmt: Any = (
        select(table, PlayerMaster, PlayerStatus, Position, Season)  # type: ignore[call-overload]
        .join(
            PlayerMaster,
            table.player_id == PlayerMaster.id,  # type: ignore[arg-type,attr-defined]
        )
        .outerjoin(
            PlayerStatus,
            PlayerMaster.id == PlayerStatus.player_id,  # type: ignore[arg-type]
        )
        .outerjoin(
            Position,
            table.position_id == Position.id,  # type: ignore[arg-type,attr-defined]
        )
        .join(
            Season,
            table.season_id == Season.id,  # type: ignore[arg-type,attr-defined]
        )
        .where(col.isnot(None))  # type: ignore[union-attr]
    )

    if years:
        stmt = stmt.where(Season.end_year.in_(years))  # type: ignore[attr-defined]

    if positions:
        stmt = stmt.where(Position.code.in_(positions))  # type: ignore[attr-defined]

    if is_active_nba is not None:
        stmt = stmt.where(PlayerStatus.is_active_nba == is_active_nba)  # type: ignore[arg-type]

    return stmt


def _order_column(metric_key: str) -> Any:
    """Return the SQLAlchemy column expression for ordering."""
    defn = METRIC_COLUMN_MAP[metric_key]
    return getattr(defn.table, defn.column)


def _row_to_entry(
    row: Any,
    metric_key: str,
    rank: int,
    percentile: float | None,
) -> LeaderboardEntry:
    """Convert a query result row to a LeaderboardEntry."""
    combine_record = row[0]
    player: PlayerMaster = row[1]
    status: PlayerStatus | None = row[2]
    position: Position | None = row[3]

    defn = METRIC_COLUMN_MAP[metric_key]
    raw_value = getattr(combine_record, defn.column)

    return LeaderboardEntry(
        rank=rank,
        player_id=player.id,  # type: ignore[arg-type]
        display_name=player.display_name or "",
        slug=player.slug or "",
        school=player.school,
        position=position.code if position else None,
        draft_year=player.draft_year,
        draft_round=player.draft_round,
        draft_pick=player.draft_pick,
        is_active_nba=bool(status and status.is_active_nba),
        raw_value=float(raw_value),
        formatted_value=_format_value(metric_key, raw_value),
        percentile=percentile,
    )


async def get_leaderboard(
    db: AsyncSession,
    metric_key: str,
    *,
    years: list[int] | None = None,
    positions: list[str] | None = None,
    is_active_nba: bool | None = None,
    limit: int = 25,
    offset: int = 0,
) -> LeaderboardResult:
    """Fetch a ranked leaderboard for a combine metric.

    Args:
        db: Async database session.
        metric_key: Key from METRIC_COLUMN_MAP (e.g., "wingspan_in").
        years: Optional year filter list (matches Season.end_year).
        positions: Optional position code filter list (e.g., ["C", "PG"]).
        is_active_nba: Optional NBA status filter (True=active, False=out).
        limit: Page size.
        offset: Page offset.

    Returns:
        LeaderboardResult with paginated entries and summary card data.
    """
    defn = METRIC_COLUMN_MAP[metric_key]
    metric_info = get_metric_info(metric_key)
    assert metric_info is not None

    base = _build_base_query(
        metric_key, years=years, positions=positions, is_active_nba=is_active_nba
    )
    order_col = _order_column(metric_key)

    # PlayerMaster.id as tiebreaker for deterministic ordering
    tiebreak = PlayerMaster.id.asc()  # type: ignore[union-attr]

    if defn.sort_direction == "asc":
        ordered = base.order_by(order_col.asc(), tiebreak)  # type: ignore[union-attr]
        reverse_ordered = base.order_by(order_col.desc(), tiebreak)  # type: ignore[union-attr]
    else:
        ordered = base.order_by(order_col.desc(), tiebreak)  # type: ignore[union-attr]
        reverse_ordered = base.order_by(order_col.asc(), tiebreak)  # type: ignore[union-attr]

    # Total count
    count_col = getattr(defn.table, defn.column)
    count_stmt = (
        select(func.count())
        .select_from(defn.table)
        .join(
            PlayerMaster,
            defn.table.player_id == PlayerMaster.id,  # type: ignore[arg-type,attr-defined]
        )
        .outerjoin(
            Position,
            defn.table.position_id == Position.id,  # type: ignore[arg-type,attr-defined]
        )
        .join(
            Season,
            defn.table.season_id == Season.id,  # type: ignore[arg-type,attr-defined]
        )
        .where(count_col.isnot(None))  # type: ignore[union-attr]
    )
    if years:
        count_stmt = count_stmt.where(Season.end_year.in_(years))  # type: ignore[attr-defined]
    if positions:
        count_stmt = count_stmt.where(Position.code.in_(positions))  # type: ignore[attr-defined]
    if is_active_nba is not None:
        count_stmt = count_stmt.outerjoin(
            PlayerStatus,
            PlayerMaster.id == PlayerStatus.player_id,  # type: ignore[arg-type]
        ).where(PlayerStatus.is_active_nba == is_active_nba)  # type: ignore[arg-type]

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    # Paginated entries
    page_stmt = ordered.offset(offset).limit(limit)
    page_result = await db.execute(page_stmt)
    rows = page_result.all()

    entries = []
    for i, row in enumerate(rows):
        rank = offset + i + 1
        pctl = round((1 - (rank - 1) / total) * 100, 1) if total > 1 else 100.0
        entries.append(_row_to_entry(row, metric_key, rank, pctl))

    # Summary cards: best (#1 in leaderboard order)
    best_result = await db.execute(ordered.limit(1))
    best_row = best_result.first()
    best = _row_to_entry(best_row, metric_key, 1, 100.0) if best_row else None

    # Summary cards: worst (last in leaderboard order)
    worst_result = await db.execute(reverse_ordered.limit(1))
    worst_row = worst_result.first()
    worst = _row_to_entry(worst_row, metric_key, total, 0.0) if worst_row else None

    # Summary cards: median (middle of sorted list)
    median_value: float | None = None
    typical: LeaderboardEntry | None = None
    if total > 0:
        median_offset = total // 2
        median_result = await db.execute(ordered.offset(median_offset).limit(1))
        median_row = median_result.first()
        if median_row:
            median_rank = median_offset + 1
            median_pctl = (
                round((1 - (median_rank - 1) / total) * 100, 1) if total > 1 else 100.0
            )
            typical = _row_to_entry(median_row, metric_key, median_rank, median_pctl)
            median_value = typical.raw_value

    return LeaderboardResult(
        entries=entries,
        total=total,
        metric=metric_info,
        best=best,
        worst=worst,
        median_value=median_value,
        typical=typical,
    )


async def get_available_years(
    db: AsyncSession, metric_key: str | None = None
) -> list[int]:
    """Get distinct years that have combine data, sorted descending.

    Args:
        db: Async database session.
        metric_key: If provided, scope to the table backing this metric.
            Otherwise unions anthro and agility seasons.
    """
    if metric_key and metric_key in METRIC_COLUMN_MAP:
        table = METRIC_COLUMN_MAP[metric_key].table
        col = getattr(table, METRIC_COLUMN_MAP[metric_key].column)
        stmt = (
            select(Season.end_year)  # type: ignore[call-overload]
            .distinct()
            .join(table, table.season_id == Season.id)  # type: ignore[arg-type,attr-defined]
            .where(col.isnot(None))  # type: ignore[union-attr]
            .order_by(Season.end_year.desc())  # type: ignore[union-attr,attr-defined]
        )
    else:
        anthro_years = select(Season.end_year).join(  # type: ignore[call-overload]
            CombineAnthro,
            CombineAnthro.season_id == Season.id,  # type: ignore[arg-type]
        )
        agility_years = select(Season.end_year).join(  # type: ignore[call-overload]
            CombineAgility,
            CombineAgility.season_id == Season.id,  # type: ignore[arg-type]
        )
        shooting_years = select(Season.end_year).join(  # type: ignore[call-overload]
            CombineShooting,
            CombineShooting.season_id == Season.id,  # type: ignore[arg-type]
        )
        combined = anthro_years.union(agility_years).union(shooting_years).subquery()
        stmt = (
            select(combined.c.end_year).distinct().order_by(combined.c.end_year.desc())
        )
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def get_available_positions(
    db: AsyncSession, metric_key: str | None = None
) -> list[tuple[str, str]]:
    """Get position codes that have combine data.

    Args:
        db: Async database session.
        metric_key: If provided, scope to the table backing this metric.
            Otherwise unions anthro and agility position_ids.

    Returns:
        List of (code, description) tuples sorted alphabetically.
    """
    if metric_key and metric_key in METRIC_COLUMN_MAP:
        table = METRIC_COLUMN_MAP[metric_key].table
        col = getattr(table, METRIC_COLUMN_MAP[metric_key].column)
        stmt = (
            select(Position.code, Position.description)  # type: ignore[call-overload]
            .distinct()
            .join(
                table,
                table.position_id == Position.id,  # type: ignore[arg-type,attr-defined]
            )
            .where(col.isnot(None))  # type: ignore[union-attr]
            .order_by(Position.code)  # type: ignore[union-attr]
        )
    else:
        anthro_positions = select(CombineAnthro.position_id).where(  # type: ignore[call-overload]
            CombineAnthro.position_id.isnot(None)  # type: ignore[union-attr]
        )
        agility_positions = select(CombineAgility.position_id).where(  # type: ignore[call-overload]
            CombineAgility.position_id.isnot(None)  # type: ignore[union-attr]
        )
        shooting_positions = select(CombineShooting.position_id).where(  # type: ignore[call-overload]
            CombineShooting.position_id.isnot(None)  # type: ignore[union-attr]
        )
        combined_pos_ids = (
            anthro_positions.union(agility_positions)
            .union(shooting_positions)
            .subquery()
        )

        stmt = (
            select(Position.code, Position.description)  # type: ignore[call-overload]
            .distinct()
            .join(
                combined_pos_ids,
                combined_pos_ids.c.position_id == Position.id,  # type: ignore[arg-type]
            )
            .order_by(Position.code)  # type: ignore[union-attr]
        )
    result = await db.execute(stmt)
    return [(row[0], row[1] or row[0]) for row in result.all()]


# === Homepage Support ===


class HomepageMetricDisplay(NamedTuple):
    """Display metadata for a metric card on the stats homepage."""

    icon: str  # HTML entity for the card icon
    superlative: str  # e.g., "Longest Wingspan"
    unit_label: str  # e.g., "wingspan", shown below the value


HOMEPAGE_METRIC_DISPLAY: dict[str, HomepageMetricDisplay] = {
    # Measurements (cyan)
    "wingspan_in": HomepageMetricDisplay("&#x1F4CF;", "Longest Wingspan", "wingspan"),
    "standing_reach_in": HomepageMetricDisplay(
        "&#x1F9CD;", "Highest Standing Reach", "reach"
    ),
    "height_w_shoes_in": HomepageMetricDisplay(
        "&#x1F4D0;", "Tallest (w/ Shoes)", "height"
    ),
    "height_wo_shoes_in": HomepageMetricDisplay(
        "&#x1F9B6;", "Tallest (Barefoot)", "barefoot"
    ),
    "weight_lb": HomepageMetricDisplay("&#x2696;", "Heaviest", "lbs"),
    "body_fat_pct": HomepageMetricDisplay("&#x1F4AA;", "Lowest Body Fat %", "body fat"),
    "hand_length_in": HomepageMetricDisplay(
        "&#x1F91A;", "Longest Hand Length", "hand length"
    ),
    "hand_width_in": HomepageMetricDisplay(
        "&#x270B;", "Largest Hand Width", "hand width"
    ),
    # Athletic Testing (amber)
    "max_vertical_in": HomepageMetricDisplay(
        "&#x1F680;", "Highest Max Vertical", "max vertical"
    ),
    "standing_vertical_in": HomepageMetricDisplay(
        "&#x2B06;", "Highest Standing Vertical", "standing vert"
    ),
    "three_quarter_sprint_s": HomepageMetricDisplay(
        "&#x26A1;", "Fastest 3/4 Sprint", "3/4 sprint"
    ),
    "lane_agility_time_s": HomepageMetricDisplay(
        "&#x1F3C3;", "Fastest Lane Agility", "lane agility"
    ),
    "shuttle_run_s": HomepageMetricDisplay(
        "&#x1F504;", "Fastest Shuttle Run", "shuttle run"
    ),
    "bench_press_reps": HomepageMetricDisplay(
        "&#x1F3CB;", "Most Bench Press Reps", "reps @ 185 lbs"
    ),
}

SHOOTING_DRILL_DISPLAY: dict[str, HomepageMetricDisplay] = {
    "spot_up": HomepageMetricDisplay("&#x1F3AF;", "Best Spot-Up Shooting", "spot-up"),
    "off_dribble": HomepageMetricDisplay(
        "&#x1F3C0;", "Best Off-Dribble", "off-dribble"
    ),
    "three_point_star": HomepageMetricDisplay(
        "&#x2B50;", "Best 3-Point Star", "3PT star"
    ),
    "free_throw": HomepageMetricDisplay("&#x1F945;", "Best Free Throw", "free throw"),
    "midrange_star": HomepageMetricDisplay(
        "&#x1F3C0;", "Best Midrange Star", "midrange star"
    ),
    "three_point_side": HomepageMetricDisplay(
        "&#x1F3C0;", "Best 3-Point Side", "3PT side"
    ),
    "midrange_side": HomepageMetricDisplay(
        "&#x1F3C0;", "Best Midrange Side", "midrange side"
    ),
}


@dataclass
class ShootingLeaderEntry:
    """Single row in a shooting drill leaderboard."""

    rank: int
    player_id: int
    display_name: str
    slug: str
    school: str | None
    position: str | None
    draft_year: int | None
    fgm: int
    fga: int
    fg_pct: float
    formatted_value: str  # e.g., "15/15"
    formatted_pct: str  # e.g., "100.0%"


@dataclass
class YearStats:
    """Player count and data availability for a single draft class year."""

    year: int
    player_count: int
    has_anthro: bool
    has_agility: bool
    has_shooting: bool


@dataclass
class HomepageData:
    """All data needed to render the stats homepage."""

    measurement_leaders: dict[str, list[LeaderboardEntry]]
    athletic_leaders: dict[str, list[LeaderboardEntry]]
    shooting_leaders: dict[str, list[ShootingLeaderEntry]]
    year_stats: list[YearStats]


# Ordered lists of metric keys for each homepage section
MEASUREMENT_KEYS = [
    "wingspan_in",
    "standing_reach_in",
    "height_w_shoes_in",
    "weight_lb",
    "hand_width_in",
    "hand_length_in",
    "body_fat_pct",
    "height_wo_shoes_in",
]

ATHLETIC_KEYS = [
    "max_vertical_in",
    "standing_vertical_in",
    "three_quarter_sprint_s",
    "lane_agility_time_s",
    "shuttle_run_s",
    "bench_press_reps",
]

SHOOTING_KEYS = [
    "spot_up",
    "off_dribble",
    "three_point_star",
    "free_throw",
    "midrange_star",
    "three_point_side",
    "midrange_side",
]


async def get_metric_leaders(
    db: AsyncSession,
    metric_key: str,
    limit: int = 4,
) -> list[LeaderboardEntry]:
    """Fetch the top N leaders for a single combine metric (anthro/agility)."""
    defn = METRIC_COLUMN_MAP[metric_key]
    base = _build_base_query(metric_key)
    order_col = _order_column(metric_key)
    tiebreak = PlayerMaster.id.asc()  # type: ignore[union-attr]

    if defn.sort_direction == "asc":
        ordered = base.order_by(order_col.asc(), tiebreak)  # type: ignore[union-attr]
    else:
        ordered = base.order_by(order_col.desc(), tiebreak)  # type: ignore[union-attr]

    result = await db.execute(ordered.limit(limit))
    rows = result.all()
    return [_row_to_entry(row, metric_key, i + 1, None) for i, row in enumerate(rows)]


async def get_shooting_leaders(
    db: AsyncSession,
    drill_key: str,
    limit: int = 4,
) -> list[ShootingLeaderEntry]:
    """Fetch the top N leaders for a shooting drill, ranked by FG%."""
    cols = SHOOTING_DRILL_COLUMNS.get(drill_key)
    if not cols:
        return []
    fgm_col_name, fga_col_name = cols
    fgm_col = getattr(CombineShooting, fgm_col_name)
    fga_col = getattr(CombineShooting, fga_col_name)

    fg_pct_expr = cast(fgm_col, Float) / cast(fga_col, Float)  # type: ignore[arg-type]

    # Select individual columns instead of the full CombineShooting entity
    # to avoid loading _pct columns that may not exist in the DB yet.
    stmt: Any = (
        select(  # type: ignore[call-overload]
            fgm_col.label("fgm"),
            fga_col.label("fga"),
            fg_pct_expr.label("fg_pct"),
            PlayerMaster.id.label("player_id"),  # type: ignore[union-attr]
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,
            PlayerMaster.draft_year,
            Position.code.label("position_code"),  # type: ignore[union-attr,attr-defined]
        )
        .select_from(CombineShooting)
        .join(
            PlayerMaster,
            CombineShooting.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .outerjoin(
            Position,
            CombineShooting.position_id == Position.id,  # type: ignore[arg-type]
        )
        .join(
            Season,
            CombineShooting.season_id == Season.id,  # type: ignore[arg-type]
        )
        .where(fgm_col.isnot(None))  # type: ignore[union-attr]
        .where(fga_col.isnot(None))  # type: ignore[union-attr]
        .where(fga_col > 0)  # type: ignore[operator]
        .order_by(
            fg_pct_expr.desc(),
            fga_col.desc(),  # type: ignore[union-attr]
            PlayerMaster.id.asc(),  # type: ignore[union-attr]
        )
        .limit(limit)
    )

    result = await db.execute(stmt)
    rows = result.all()

    entries: list[ShootingLeaderEntry] = []
    for i, row in enumerate(rows):
        fgm = row.fgm
        fga = row.fga
        pct = row.fg_pct
        entries.append(
            ShootingLeaderEntry(
                rank=i + 1,
                player_id=row.player_id,
                display_name=row.display_name or "",
                slug=row.slug or "",
                school=row.school,
                position=row.position_code,
                draft_year=row.draft_year,
                fgm=fgm,
                fga=fga,
                fg_pct=float(pct) if pct is not None else 0.0,
                formatted_value=f"{fgm}/{fga}",
                formatted_pct=f"{float(pct) * 100:.1f}%" if pct is not None else "0.0%",
            )
        )
    return entries


async def get_year_player_counts(db: AsyncSession) -> list[YearStats]:
    """Get player counts and data type availability per draft class year."""
    # Anthro counts per year
    anthro_stmt: Any = (
        select(  # type: ignore[call-overload]
            Season.end_year,
            func.count(func.distinct(CombineAnthro.player_id)),
        )
        .join(Season, CombineAnthro.season_id == Season.id)  # type: ignore[arg-type]
        .group_by(Season.end_year)
    )
    anthro_result = await db.execute(anthro_stmt)
    anthro_counts: dict[int, int] = {row[0]: row[1] for row in anthro_result.all()}

    # Agility counts per year
    agility_stmt: Any = (
        select(  # type: ignore[call-overload]
            Season.end_year,
            func.count(func.distinct(CombineAgility.player_id)),
        )
        .join(Season, CombineAgility.season_id == Season.id)  # type: ignore[arg-type]
        .group_by(Season.end_year)
    )
    agility_result = await db.execute(agility_stmt)
    agility_counts: dict[int, int] = {row[0]: row[1] for row in agility_result.all()}

    # Shooting counts per year
    shooting_stmt: Any = (
        select(  # type: ignore[call-overload]
            Season.end_year,
            func.count(func.distinct(CombineShooting.player_id)),
        )
        .join(Season, CombineShooting.season_id == Season.id)  # type: ignore[arg-type]
        .group_by(Season.end_year)
    )
    shooting_result = await db.execute(shooting_stmt)
    shooting_counts: dict[int, int] = {row[0]: row[1] for row in shooting_result.all()}

    all_years = sorted(
        set(anthro_counts) | set(agility_counts) | set(shooting_counts),
        reverse=True,
    )

    return [
        YearStats(
            year=y,
            player_count=max(
                anthro_counts.get(y, 0),
                agility_counts.get(y, 0),
                shooting_counts.get(y, 0),
            ),
            has_anthro=y in anthro_counts,
            has_agility=y in agility_counts,
            has_shooting=y in shooting_counts,
        )
        for y in all_years
    ]


async def get_homepage_data(db: AsyncSession) -> HomepageData:
    """Gather all data for the stats homepage."""
    measurement_leaders: dict[str, list[LeaderboardEntry]] = {}
    for key in MEASUREMENT_KEYS:
        measurement_leaders[key] = await get_metric_leaders(db, key)

    athletic_leaders: dict[str, list[LeaderboardEntry]] = {}
    for key in ATHLETIC_KEYS:
        athletic_leaders[key] = await get_metric_leaders(db, key)

    shooting_leaders: dict[str, list[ShootingLeaderEntry]] = {}
    for key in SHOOTING_KEYS:
        shooting_leaders[key] = await get_shooting_leaders(db, key)

    year_stats = await get_year_player_counts(db)

    return HomepageData(
        measurement_leaders=measurement_leaders,
        athletic_leaders=athletic_leaders,
        shooting_leaders=shooting_leaders,
        year_stats=year_stats,
    )

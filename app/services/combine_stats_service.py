"""Public stats service for combine leaderboards and metric exploration.

Provides leaderboard queries, metric metadata, and summary card data
for the /stats section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
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
    highest: LeaderboardEntry | None
    lowest: LeaderboardEntry | None
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
    else:
        result = str(value) if value is not None else None
    return result or str(value)


def _build_base_query(
    metric_key: str,
    *,
    year: int | None = None,
    position: str | None = None,
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

    if year is not None:
        stmt = stmt.where(Season.end_year == year)  # type: ignore[arg-type]

    if position is not None:
        stmt = stmt.where(Position.code == position)  # type: ignore[arg-type]

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
    year: int | None = None,
    position: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> LeaderboardResult:
    """Fetch a ranked leaderboard for a combine metric.

    Args:
        db: Async database session.
        metric_key: Key from METRIC_COLUMN_MAP (e.g., "wingspan_in").
        year: Optional year filter (matches Season.end_year).
        position: Optional position code filter (e.g., "C", "PG").
        limit: Page size.
        offset: Page offset.

    Returns:
        LeaderboardResult with paginated entries and summary card data.
    """
    defn = METRIC_COLUMN_MAP[metric_key]
    metric_info = get_metric_info(metric_key)
    assert metric_info is not None

    base = _build_base_query(metric_key, year=year, position=position)
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
    if year is not None:
        count_stmt = count_stmt.where(Season.end_year == year)  # type: ignore[arg-type]
    if position is not None:
        count_stmt = count_stmt.where(Position.code == position)  # type: ignore[arg-type]

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

    # Summary cards: highest (#1)
    highest_result = await db.execute(ordered.limit(1))
    highest_row = highest_result.first()
    highest = _row_to_entry(highest_row, metric_key, 1, 100.0) if highest_row else None

    # Summary cards: lowest (last)
    lowest_result = await db.execute(reverse_ordered.limit(1))
    lowest_row = lowest_result.first()
    lowest = _row_to_entry(lowest_row, metric_key, total, 0.0) if lowest_row else None

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
        highest=highest,
        lowest=lowest,
        median_value=median_value,
        typical=typical,
    )


async def get_available_years(db: AsyncSession) -> list[int]:
    """Get distinct years that have combine data, sorted descending.

    Unions anthro and agility seasons so both data sources contribute.
    """
    anthro_years = select(Season.end_year).join(  # type: ignore[call-overload]
        CombineAnthro,
        CombineAnthro.season_id == Season.id,  # type: ignore[arg-type]
    )
    agility_years = select(Season.end_year).join(  # type: ignore[call-overload]
        CombineAgility,
        CombineAgility.season_id == Season.id,  # type: ignore[arg-type]
    )
    combined = anthro_years.union(agility_years).subquery()
    stmt = select(combined.c.end_year).distinct().order_by(combined.c.end_year.desc())
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def get_available_positions(db: AsyncSession) -> list[tuple[str, str]]:
    """Get position codes that have combine data.

    Unions anthro and agility position_ids from combine tables directly.

    Returns:
        List of (code, description) tuples sorted alphabetically.
    """
    anthro_positions = select(CombineAnthro.position_id).where(  # type: ignore[call-overload]
        CombineAnthro.position_id.isnot(None)  # type: ignore[union-attr]
    )
    agility_positions = select(CombineAgility.position_id).where(  # type: ignore[call-overload]
        CombineAgility.position_id.isnot(None)  # type: ignore[union-attr]
    )
    combined_pos_ids = anthro_positions.union(agility_positions).subquery()

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

"""Service helpers for player metric retrieval."""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple, cast

from sqlalchemy import desc, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricCategory, MetricSource
from app.models.position_taxonomy import derive_position_tags
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting, SHOOTING_DRILL_COLUMNS
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.schemas.seasons import Season


class PlayerMetricsResult(dict):
    """Typed dict-style response for player metrics."""

    metrics: List[dict]
    snapshot_id: Optional[int]
    population_size: Optional[int]


_CATEGORY_TO_SOURCE: dict[MetricCategory, MetricSource] = {
    MetricCategory.anthropometrics: MetricSource.combine_anthro,
    MetricCategory.combine_performance: MetricSource.combine_agility,
    MetricCategory.shooting: MetricSource.combine_shooting,
    MetricCategory.advanced_stats: MetricSource.advanced_stats,
}


async def _resolve_player_id(db: AsyncSession, slug: str) -> Optional[int]:
    stmt = select(PlayerMaster.id).where(PlayerMaster.slug == slug)  # type: ignore[arg-type,call-overload]
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _resolve_parent_scope(db: AsyncSession, player_id: int) -> Optional[str]:
    """Return the parent position scope (guard/wing/forward/big) if available."""
    stmt = (
        select(PlayerStatus.raw_position, Position.parents)  # type: ignore[call-overload]
        .select_from(PlayerStatus)
        .outerjoin(Position, Position.id == PlayerStatus.position_id)
        .where(PlayerStatus.player_id == player_id)
    )
    result = await db.execute(stmt)
    row = result.mappings().first()
    if not row:
        return None

    parents: Optional[Sequence[str]] = row.get("parents")
    if parents:
        # Use the first parent token
        return list(parents)[0]

    raw_position: Optional[str] = row.get("raw_position")
    if raw_position:
        _, derived_parents = derive_position_tags(raw_position)
        if derived_parents:
            return derived_parents[0]
    return None


async def _resolve_parent_scope_for_source(
    db: AsyncSession,
    *,
    player_id: int,
    source: MetricSource,
    season_id: Optional[int],
) -> Optional[str]:
    """Resolve parent scope from the source table used to compute the snapshot.

    We prefer the position attached to the underlying combine row (anthro/agility/shooting)
    because snapshot cohorts are filtered by those tables during metric computation.
    """
    table: Any = {
        MetricSource.combine_anthro: CombineAnthro,
        MetricSource.combine_agility: CombineAgility,
        MetricSource.combine_shooting: CombineShooting,
    }.get(source)
    if table is None:
        return await _resolve_parent_scope(db, player_id)

    stmt = (
        select(table.raw_position, Position.parents)  # type: ignore[call-overload]
        .select_from(table)
        .outerjoin(Position, Position.id == table.position_id)  # type: ignore[attr-defined]
        .join(Season, Season.id == table.season_id)  # type: ignore[attr-defined]
        .where(table.player_id == player_id)  # type: ignore[attr-defined]
        .order_by(desc(Season.start_year))  # type: ignore[arg-type]
        .limit(1)
    )
    if season_id is not None:
        stmt = stmt.where(table.season_id == season_id)  # type: ignore[attr-defined,arg-type]

    result = await db.execute(stmt)
    row = result.mappings().first()
    if not row:
        return await _resolve_parent_scope(db, player_id)

    parents: Optional[Sequence[str]] = row.get("parents")
    if parents:
        return list(parents)[0]

    raw_position: Optional[str] = row.get("raw_position")
    if raw_position:
        _, derived_parents = derive_position_tags(raw_position)
        if derived_parents:
            return derived_parents[0]
    return None


async def _latest_season_id(db: AsyncSession, player_id: int) -> Optional[int]:
    """Find the most recent combine season for the player across sources."""
    season_candidates: List[tuple[int, int]] = []
    sources: Iterable[tuple[object, object]] = (
        (CombineAnthro, CombineAnthro.season_id),
        (CombineAgility, CombineAgility.season_id),
        (CombineShooting, CombineShooting.season_id),
    )
    for table, season_col in sources:
        stmt = (
            select(season_col, Season.start_year)  # type: ignore[call-overload]
            .select_from(table)
            .join(Season, Season.id == season_col)
            .where(table.player_id == player_id)  # type: ignore[attr-defined]
            .order_by(desc(Season.start_year))  # type: ignore[arg-type]
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.first()
        if row:
            season_candidates.append((row[0], row[1]))

    if not season_candidates:
        return None

    season_candidates.sort(key=lambda item: item[1], reverse=True)
    return season_candidates[0][0]


async def _select_snapshot(
    db: AsyncSession,
    cohort: CohortType,
    source: MetricSource,
    season_id: Optional[int],
    parent_scope: Optional[str],
    prefer_parent: bool,
) -> Optional[MetricSnapshot]:
    """Select a snapshot with preference for parent scope, fallback to baseline."""

    def _order(stmt):
        return stmt.order_by(
            MetricSnapshot.is_current.desc(),  # type: ignore[attr-defined]
            MetricSnapshot.version.desc(),  # type: ignore[attr-defined]
        ).limit(1)

    base_filters = [
        MetricSnapshot.cohort == cohort,  # type: ignore[arg-type]
        MetricSnapshot.source == source,  # type: ignore[arg-type]
    ]
    if season_id is not None:
        base_filters.append(MetricSnapshot.season_id == season_id)  # type: ignore[arg-type]

    if prefer_parent and parent_scope:
        stmt = select(MetricSnapshot).where(*base_filters)  # type: ignore[arg-type]
        stmt = stmt.where(MetricSnapshot.position_scope_parent == parent_scope)  # type: ignore[arg-type]
        result = await db.execute(_order(stmt))
        snap = result.scalar_one_or_none()
        if snap:
            return snap

    # Fallback to baseline (all positions)
    stmt = select(MetricSnapshot).where(*base_filters)  # type: ignore[arg-type]
    stmt = stmt.where(
        MetricSnapshot.position_scope_parent.is_(None),  # type: ignore[union-attr]
        MetricSnapshot.position_scope_fine.is_(None),  # type: ignore[union-attr]
    )
    result = await db.execute(_order(stmt))
    return result.scalar_one_or_none()


async def get_player_metrics(
    db: AsyncSession,
    slug: str,
    cohort: CohortType,
    category: MetricCategory,
    position_adjusted: bool = True,
    season_id: Optional[int] = None,
) -> PlayerMetricsResult:
    """Fetch metric rows for a player and cohort/category combination."""
    player_id = await _resolve_player_id(db, slug)
    if player_id is None:
        raise ValueError("player_not_found")

    source = _CATEGORY_TO_SOURCE.get(category)
    if source is None:
        return PlayerMetricsResult(metrics=[], snapshot_id=None)

    effective_season_id = season_id
    if cohort == CohortType.current_draft and effective_season_id is None:
        effective_season_id = await _latest_season_id(db, player_id)

    parent_scope = (
        await _resolve_parent_scope_for_source(
            db,
            player_id=player_id,
            source=source,
            season_id=effective_season_id,
        )
        if position_adjusted
        else None
    )

    snapshot = await _select_snapshot(
        db,
        cohort=cohort,
        source=source,
        season_id=effective_season_id,
        parent_scope=parent_scope,
        prefer_parent=position_adjusted,
    )

    if not snapshot:
        return PlayerMetricsResult(metrics=[], snapshot_id=None, population_size=None)

    async def _metric_population_size(metric_key: str) -> Optional[int]:
        """Return the baseline population size used for the metric's distribution."""

        if snapshot is None:
            return None

        source = snapshot.source
        stmt = None

        def _apply_position_scope(base_stmt, table):
            if snapshot.position_scope_fine is not None:
                return base_stmt.join(Position, Position.id == table.position_id).where(
                    Position.code == snapshot.position_scope_fine
                )
            if snapshot.position_scope_parent is not None:
                return base_stmt.join(Position, Position.id == table.position_id).where(
                    Position.parents.contains([snapshot.position_scope_parent])
                )
            return base_stmt

        def _apply_cohort_scope(base_stmt, table):
            if snapshot.season_id is not None:
                base_stmt = base_stmt.where(table.season_id == snapshot.season_id)
            if snapshot.cohort == CohortType.current_nba:
                base_stmt = base_stmt.outerjoin(
                    PlayerStatus, PlayerStatus.player_id == table.player_id
                ).where(PlayerStatus.is_active_nba.is_(True))
            elif snapshot.cohort == CohortType.all_time_nba:
                base_stmt = base_stmt.outerjoin(
                    PlayerStatus, PlayerStatus.player_id == table.player_id
                ).where(
                    or_(
                        PlayerStatus.is_active_nba.is_(True),
                        PlayerStatus.nba_last_season.is_not(None),
                    )
                )
            return base_stmt

        if source == MetricSource.combine_shooting and metric_key.endswith("_fg_pct"):
            drill = metric_key[: -len("_fg_pct")]
            drill_cols = SHOOTING_DRILL_COLUMNS.get(drill)
            if drill_cols is None:
                return None
            fgm_col, fga_col = drill_cols
            fgm = getattr(CombineShooting, fgm_col, None)
            fga = getattr(CombineShooting, fga_col, None)
            if fgm is None or fga is None:
                return None
            stmt = select(
                func.count(distinct(cast(Any, CombineShooting).player_id))
            ).select_from(CombineShooting)
            stmt = _apply_cohort_scope(stmt, CombineShooting)
            stmt = _apply_position_scope(stmt, CombineShooting)
            stmt = stmt.where(
                fgm.is_not(None),  # type: ignore[union-attr]
                fga.is_not(None),  # type: ignore[union-attr]
                fga != 0,  # type: ignore[comparison-overlap]
            )
        elif source == MetricSource.combine_anthro:
            column = getattr(CombineAnthro, metric_key, None)
            if column is None:
                return None
            stmt = select(
                func.count(distinct(cast(Any, CombineAnthro).player_id))
            ).select_from(CombineAnthro)
            stmt = _apply_cohort_scope(stmt, CombineAnthro)
            stmt = _apply_position_scope(stmt, CombineAnthro)
            stmt = stmt.where(column.is_not(None))  # type: ignore[union-attr]
        elif source == MetricSource.combine_agility:
            column = getattr(CombineAgility, metric_key, None)
            if column is None:
                return None
            stmt = select(
                func.count(distinct(cast(Any, CombineAgility).player_id))
            ).select_from(CombineAgility)
            stmt = _apply_cohort_scope(stmt, CombineAgility)
            stmt = _apply_position_scope(stmt, CombineAgility)
            stmt = stmt.where(column.is_not(None))  # type: ignore[union-attr]
        else:
            return snapshot.population_size

        result = await db.execute(stmt)
        count = result.scalar()
        if count is None:
            return None
        count_int = int(count)
        return count_int if count_int > 0 else None

    def _rows_stmt(snapshot_id: int):
        stmt: Any = select(
            MetricDefinition.metric_key,
            MetricDefinition.display_name,
            MetricDefinition.unit,
            PlayerMetricValue.raw_value,
            PlayerMetricValue.percentile,
            PlayerMetricValue.rank,
            PlayerMetricValue.extra_context,
        )  # type: ignore[call-overload, misc]
        return (
            stmt.join(
                MetricDefinition,
                MetricDefinition.id == PlayerMetricValue.metric_definition_id,
            )
            .where(PlayerMetricValue.snapshot_id == snapshot_id)
            .where(PlayerMetricValue.player_id == player_id)
            .where(MetricDefinition.category == category)  # type: ignore[arg-type]
            .order_by(MetricDefinition.display_name)
        )

    if snapshot.id is None:
        return PlayerMetricsResult(metrics=[], snapshot_id=None, population_size=None)

    result = await db.execute(_rows_stmt(snapshot.id))
    rows = result.all()
    if not rows and position_adjusted and parent_scope:
        # If the player is not represented in the scoped snapshot (common when
        # PlayerStatus position differs from the combine row position), fall back
        # to baseline for this cohort/source.
        fallback_snapshot = await _select_snapshot(
            db,
            cohort=cohort,
            source=source,
            season_id=effective_season_id,
            parent_scope=None,
            prefer_parent=False,
        )
        if fallback_snapshot and fallback_snapshot.id is not None:
            snapshot = fallback_snapshot
            result = await db.execute(_rows_stmt(fallback_snapshot.id))
            rows = result.all()

    metrics: List[dict] = []
    population_cache: dict[str, Optional[int]] = {}
    for row in rows:
        display_value, display_unit = format_metric_value(
            metric_key=row.metric_key,
            unit=row.unit,
            raw_value=row.raw_value,
        )
        percentile_val = row.percentile
        metric_population: Optional[int] = None
        if row.extra_context and isinstance(row.extra_context, dict):
            metric_population = row.extra_context.get("population_size")
            if metric_population is not None:
                metric_population = int(metric_population)
        if metric_population is None:
            metric_population = population_cache.get(row.metric_key)
        if metric_population is None and row.metric_key not in population_cache:
            metric_population = await _metric_population_size(row.metric_key)
            population_cache[row.metric_key] = metric_population

        metrics.append(
            {
                "metric": row.display_name,
                "value": display_value,
                "unit": display_unit,
                "percentile": int(round(percentile_val))
                if percentile_val is not None
                else None,
                "rank": row.rank,
                "population_size": metric_population,
            }
        )

    return PlayerMetricsResult(
        metrics=metrics,
        snapshot_id=snapshot.id,
        population_size=snapshot.population_size,
    )


def format_metric_value(
    metric_key: str,
    unit: Optional[str],
    raw_value: Optional[float],
) -> Tuple[Optional[str], str]:
    """Return display value and unit with DraftGuru-friendly formatting."""
    if raw_value is None:
        return None, unit or ""

    height_like_keys = {
        "height_w_shoes_in",
        "height_wo_shoes_in",
        "standing_reach_in",
        "wingspan_in",
    }
    if metric_key in height_like_keys:
        return _format_inches_to_feet(raw_value), ""

    if unit == "percent":
        return _format_number(raw_value, decimals=1), "%"

    if unit == "pounds":
        return _format_number(raw_value, decimals=1), " lbs"

    inch_like_keys = {
        "standing_vertical_in",
        "max_vertical_in",
        "hand_length_in",
        "hand_width_in",
    }
    if unit == "inches" or metric_key in inch_like_keys:
        return _format_number(raw_value, decimals=2), " in"

    if unit == "seconds":
        return _format_number(raw_value, decimals=2), " sec"

    return _format_number(raw_value, decimals=2), (unit or "")


def _format_number(value: float, decimals: int = 2) -> str:
    """Format a float with trimmed trailing zeros."""
    fmt = f"{{:.{decimals}f}}"
    text = fmt.format(value).rstrip("0").rstrip(".")
    return text


def _format_inches_to_feet(raw_inches: float) -> str:
    """Convert inches to feet'inches\" with half-inch precision."""
    rounded = round(raw_inches * 2) / 2
    feet = int(rounded) // 12
    inches = rounded % 12
    if inches == int(inches):
        return f"{feet}'{int(inches)}\""
    return f"{feet}'{inches}\""

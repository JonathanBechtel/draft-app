from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    SimilarityDimension,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting
from app.schemas.metrics import MetricSnapshot, PlayerSimilarity
from app.schemas.players_master import PlayerMaster
from app.schemas.seasons import Season
from app.services.metrics_service import format_metric_value


CATEGORY_TO_SOURCE: dict[MetricCategory, MetricSource] = {
    MetricCategory.anthropometrics: MetricSource.combine_anthro,
    MetricCategory.combine_performance: MetricSource.combine_agility,
    MetricCategory.shooting: MetricSource.combine_shooting,
    MetricCategory.advanced_stats: MetricSource.advanced_stats,
}

CATEGORY_TO_SIMILARITY_DIMENSION: dict[MetricCategory, SimilarityDimension] = {
    MetricCategory.anthropometrics: SimilarityDimension.anthro,
    MetricCategory.combine_performance: SimilarityDimension.combine,
    MetricCategory.shooting: SimilarityDimension.shooting,
}

ANTHRO_SPECS: Tuple[dict, ...] = (
    {
        "metric_key": "wingspan_in",
        "display": "Wingspan",
        "unit": "inches",
        "lower": False,
    },
    {
        "metric_key": "standing_reach_in",
        "display": "Standing Reach",
        "unit": "inches",
        "lower": False,
    },
    {
        "metric_key": "height_w_shoes_in",
        "display": "Height (With Shoes)",
        "unit": "inches",
        "lower": False,
    },
    {
        "metric_key": "height_wo_shoes_in",
        "display": "Height (Without Shoes)",
        "unit": "inches",
        "lower": False,
    },
    {"metric_key": "weight_lb", "display": "Weight", "unit": "pounds", "lower": False},
    {
        "metric_key": "body_fat_pct",
        "display": "Body Fat",
        "unit": "percent",
        "lower": True,
    },
    {
        "metric_key": "hand_length_in",
        "display": "Hand Length",
        "unit": "inches",
        "lower": False,
    },
    {
        "metric_key": "hand_width_in",
        "display": "Hand Width",
        "unit": "inches",
        "lower": False,
    },
)

AGILITY_SPECS: Tuple[dict, ...] = (
    {
        "metric_key": "lane_agility_time_s",
        "display": "Lane Agility Time",
        "unit": "seconds",
        "lower": True,
    },
    {
        "metric_key": "shuttle_run_s",
        "display": "Shuttle Run",
        "unit": "seconds",
        "lower": True,
    },
    {
        "metric_key": "three_quarter_sprint_s",
        "display": "Three-Quarter Sprint",
        "unit": "seconds",
        "lower": True,
    },
    {
        "metric_key": "standing_vertical_in",
        "display": "Standing Vertical",
        "unit": "inches",
        "lower": False,
    },
    {
        "metric_key": "max_vertical_in",
        "display": "Max Vertical",
        "unit": "inches",
        "lower": False,
    },
    {
        "metric_key": "bench_press_reps",
        "display": "Bench Press Reps",
        "unit": None,
        "lower": False,
    },
)

SHOOTING_SPECS: Tuple[dict, ...] = (
    {
        "metric_key": "off_dribble",
        "display": "Off-Dribble",
        "unit": None,
        "lower": False,
    },
    {"metric_key": "spot_up", "display": "Spot-Up", "unit": None, "lower": False},
    {
        "metric_key": "three_point_star",
        "display": "3PT Star Drill",
        "unit": None,
        "lower": False,
    },
    {
        "metric_key": "midrange_star",
        "display": "Mid-Range Star",
        "unit": None,
        "lower": False,
    },
    {
        "metric_key": "three_point_side",
        "display": "3PT Side Drill",
        "unit": None,
        "lower": False,
    },
    {
        "metric_key": "midrange_side",
        "display": "Mid-Range Side",
        "unit": None,
        "lower": False,
    },
    {
        "metric_key": "free_throw",
        "display": "Free Throws",
        "unit": None,
        "lower": False,
    },
)

CATEGORY_SPECS: dict[MetricCategory, Tuple[dict, ...]] = {
    MetricCategory.anthropometrics: ANTHRO_SPECS,
    MetricCategory.combine_performance: AGILITY_SPECS,
    MetricCategory.shooting: SHOOTING_SPECS,
}


async def _resolve_player(db: AsyncSession, slug: str) -> Tuple[int, str, str]:
    stmt = select(PlayerMaster.id, PlayerMaster.display_name, PlayerMaster.slug).where(  # type: ignore[call-overload]
        PlayerMaster.slug == slug
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise ValueError("player_not_found")
    return row.id, row.display_name, row.slug


async def _select_similarity_snapshot(
    db: AsyncSession, source: MetricSource
) -> Optional[MetricSnapshot]:
    """Pick the active snapshot for a source to read similarity from, preferring global scope."""
    # Prefer explicitly global snapshots
    stmt_global = (
        select(MetricSnapshot)
        .where(MetricSnapshot.source == source)  # type: ignore[arg-type]
        .where(MetricSnapshot.cohort == CohortType.global_scope)  # type: ignore[arg-type]
        .where(MetricSnapshot.is_current.is_(True))  # type: ignore[attr-defined]
        .order_by(MetricSnapshot.version.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    result = await db.execute(stmt_global)  # type: ignore[var-annotated]
    snapshot = result.scalar_one_or_none()
    if snapshot:
        return snapshot

    # Fallback to any current snapshot for the source
    stmt = (
        select(MetricSnapshot)
        .where(MetricSnapshot.source == source)  # type: ignore[arg-type]
        .where(MetricSnapshot.is_current.is_(True))  # type: ignore[attr-defined]
        .order_by(MetricSnapshot.version.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    result = await db.execute(stmt)  # type: ignore[var-annotated]
    return result.scalar_one_or_none()


def _get_table_for_category(category: MetricCategory):
    """Return the SQLModel table class for a given category."""
    if category == MetricCategory.anthropometrics:
        return CombineAnthro
    elif category == MetricCategory.shooting:
        return CombineShooting
    else:
        return CombineAgility


def _compute_shooting_percentages(
    row_data: Dict[str, Optional[float]],
) -> Dict[str, Optional[float]]:
    """Convert FGM/FGA pairs to shooting percentages for each drill."""
    from app.schemas.combine_shooting import SHOOTING_DRILL_COLUMNS

    result: Dict[str, Optional[float]] = {}
    for drill_key, (fgm_col, fga_col) in SHOOTING_DRILL_COLUMNS.items():
        fgm = row_data.get(fgm_col)
        fga = row_data.get(fga_col)
        if fgm is not None and fga is not None and fga > 0:
            pct = (fgm / fga) * 100
            result[drill_key] = pct
            # Also store FGM/FGA for display formatting
            result[f"{drill_key}_fgm"] = fgm
            result[f"{drill_key}_fga"] = fga
        else:
            result[drill_key] = None
    return result


async def _fetch_metric_rows(
    db: AsyncSession,
    category: MetricCategory,
    player_ids: Tuple[int, int],
) -> Dict[int, Dict[str, Optional[float]]]:
    """Fetch raw combine metrics for each player keyed by metric_key."""
    table = _get_table_for_category(category)
    values: Dict[int, Dict[str, Optional[float]]] = {}

    for player_id in player_ids:
        stmt = (
            select(table)
            .join(Season, Season.id == table.season_id)  # type: ignore[arg-type]
            .where(table.player_id == player_id)  # type: ignore[arg-type]
            .order_by(Season.start_year.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            values[player_id] = {}
            continue
        row_data = row.dict()
        if category == MetricCategory.shooting:
            # Convert FGM/FGA pairs to percentages
            values[player_id] = _compute_shooting_percentages(row_data)
        else:
            values[player_id] = row_data
    return values


def _format_shooting_display(
    drill_key: str,
    pct: float,
    player_metrics: Dict[str, Optional[float]],
) -> str:
    """Format shooting drill as 'FGM/FGA (pct%)'."""
    fgm = player_metrics.get(f"{drill_key}_fgm")
    fga = player_metrics.get(f"{drill_key}_fga")
    if fgm is not None and fga is not None:
        return f"{int(fgm)}/{int(fga)} ({pct:.0f}%)"
    return f"{pct:.0f}%"


def _build_shared_metrics(
    metrics: Dict[int, Dict[str, Optional[float]]],
    player_a_id: int,
    player_b_id: int,
    category: MetricCategory,
) -> List[dict]:
    specs = CATEGORY_SPECS.get(category, ())
    shared: List[dict] = []
    for spec in specs:
        key = spec["metric_key"]
        raw_a = metrics.get(player_a_id, {}).get(key)
        raw_b = metrics.get(player_b_id, {}).get(key)
        if raw_a is None or raw_b is None:
            continue

        # Handle shooting display format differently
        display_a: Optional[str]
        display_b: Optional[str]
        unit: str
        if category == MetricCategory.shooting:
            display_a = _format_shooting_display(
                key, raw_a, metrics.get(player_a_id, {})
            )
            display_b = _format_shooting_display(
                key, raw_b, metrics.get(player_b_id, {})
            )
            unit = ""
        else:
            display_a, unit = format_metric_value(key, spec["unit"], raw_a)
            display_b, _ = format_metric_value(key, spec["unit"], raw_b)

        shared.append(
            {
                "metric": spec["display"],
                "metric_key": key,
                "unit": unit,
                "raw_value_a": raw_a,
                "raw_value_b": raw_b,
                "display_value_a": display_a,
                "display_value_b": display_b,
                "lower_is_better": bool(spec.get("lower", False)),
            }
        )
    return shared


async def _fetch_similarity(
    db: AsyncSession,
    snapshot_id: int,
    player_a_id: int,
    player_b_id: int,
    category: MetricCategory,
) -> Optional[dict]:
    dimension = CATEGORY_TO_SIMILARITY_DIMENSION.get(category)
    if not dimension:
        return None

    stmt = (
        select(PlayerSimilarity)
        .where(PlayerSimilarity.snapshot_id == snapshot_id)  # type: ignore[arg-type]
        .where(PlayerSimilarity.dimension == dimension)  # type: ignore[arg-type]
        .where(
            or_(
                (PlayerSimilarity.anchor_player_id == player_a_id)  # type: ignore[arg-type]
                & (PlayerSimilarity.comparison_player_id == player_b_id),
                (PlayerSimilarity.anchor_player_id == player_b_id)  # type: ignore[arg-type]
                & (PlayerSimilarity.comparison_player_id == player_a_id),
            )
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        return None
    return {
        "score": row.similarity_score,
        "overlap_pct": row.overlap_pct,
    }


async def get_head_to_head_comparison(
    db: AsyncSession,
    player_a_slug: str,
    player_b_slug: str,
    category: MetricCategory,
) -> dict:
    player_a_id, player_a_name, player_a_slug = await _resolve_player(db, player_a_slug)
    player_b_id, player_b_name, player_b_slug = await _resolve_player(db, player_b_slug)

    source = CATEGORY_TO_SOURCE.get(category)
    if source is None:
        return {
            "category": category,
            "player_a": {"slug": player_a_slug, "name": player_a_name},
            "player_b": {"slug": player_b_slug, "name": player_b_name},
            "metrics": [],
            "similarity": None,
        }

    metrics_raw = await _fetch_metric_rows(db, category, (player_a_id, player_b_id))
    metrics = _build_shared_metrics(metrics_raw, player_a_id, player_b_id, category)

    # Similarity remains snapshot-based; optional best-effort retrieval
    snapshot = await _select_similarity_snapshot(db, source)
    similarity = None
    if snapshot:
        similarity = await _fetch_similarity(
            db,
            snapshot.id,  # type: ignore[arg-type]
            player_a_id,
            player_b_id,
            category,
        )

    return {
        "category": category,
        "player_a": {"slug": player_a_slug, "name": player_a_name},
        "player_b": {"slug": player_b_slug, "name": player_b_name},
        "metrics": metrics,
        "similarity": similarity,
    }

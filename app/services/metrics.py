"""
Metrics service module.

Business logic for player metrics, percentiles, and similarity queries.
"""

from typing import Any, Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.metrics import (
    MetricDefinition,
    MetricSnapshot,
    PlayerMetricValue,
    PlayerSimilarity,
)
from app.models.fields import MetricCategory, SimilarityDimension


def get_percentile_tier(percentile: Optional[float]) -> str:
    """
    Map percentile value to display tier.

    Args:
        percentile: Percentile value (0-100)

    Returns:
        Tier classification string
    """
    if percentile is None:
        return "unknown"
    if percentile >= 90:
        return "elite"
    if percentile >= 75:
        return "above-average"
    if percentile >= 50:
        return "average"
    if percentile >= 25:
        return "below-average"
    return "poor"


def get_similarity_badge_class(score: float) -> str:
    """
    Map similarity score to CSS class.

    Args:
        score: Similarity score (0-100)

    Returns:
        CSS class name for badge styling
    """
    if score >= 90:
        return "similarity-badge--high"
    if score >= 75:
        return "similarity-badge--good"
    if score >= 60:
        return "similarity-badge--moderate"
    return "similarity-badge--weak"


async def get_current_snapshot(
    db: AsyncSession,
    source: Optional[str] = None,
) -> Optional[MetricSnapshot]:
    """
    Get the current active metric snapshot.

    Args:
        db: Database session
        source: Optional source filter

    Returns:
        MetricSnapshot instance or None
    """
    query: Any = select(MetricSnapshot).where(
        MetricSnapshot.is_current.is_(True)  # type: ignore[attr-defined, union-attr]
    )

    if source:
        query = query.where(MetricSnapshot.source == source)  # type: ignore[arg-type]

    query = query.order_by(
        MetricSnapshot.calculated_at.desc()  # type: ignore[attr-defined, union-attr]
    )

    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_player_metrics(
    db: AsyncSession,
    player_id: int,
    category: Optional[MetricCategory] = None,
    snapshot_id: Optional[int] = None,
) -> list[dict]:
    """
    Get metric values for a player.

    Args:
        db: Database session
        player_id: Player's unique identifier
        category: Optional metric category filter
        snapshot_id: Optional snapshot ID (defaults to current)

    Returns:
        List of metric dictionaries with values and percentiles
    """
    # Get snapshot ID if not provided
    if snapshot_id is None:
        snapshot = await get_current_snapshot(db)
        if not snapshot:
            return []
        snapshot_id = snapshot.id

    # Build query for metrics
    query: Any = (
        select(PlayerMetricValue, MetricDefinition)
        .join(
            MetricDefinition,
            PlayerMetricValue.metric_definition_id  # type: ignore[arg-type]
            == MetricDefinition.id,
        )
        .where(
            and_(
                PlayerMetricValue.player_id == player_id,  # type: ignore[arg-type]
                PlayerMetricValue.snapshot_id == snapshot_id,  # type: ignore[arg-type]
            )
        )
    )

    if category:
        query = query.where(MetricDefinition.category == category)  # type: ignore[arg-type]

    query = query.order_by(MetricDefinition.display_name)

    result = await db.execute(query)
    rows = result.all()

    metrics = []
    for value, definition in rows:
        metrics.append(
            {
                "metric_key": definition.metric_key,
                "display_name": definition.display_name,
                "short_label": definition.short_label,
                "category": definition.category.value if definition.category else None,
                "unit": definition.unit,
                "raw_value": value.raw_value,
                "rank": value.rank,
                "percentile": value.percentile,
                "z_score": value.z_score,
                "tier": get_percentile_tier(value.percentile),
            }
        )

    return metrics


async def get_metrics_by_category(
    db: AsyncSession,
    player_id: int,
    snapshot_id: Optional[int] = None,
) -> dict[str, list[dict]]:
    """
    Get player metrics grouped by category.

    Args:
        db: Database session
        player_id: Player's unique identifier
        snapshot_id: Optional snapshot ID (defaults to current)

    Returns:
        Dictionary mapping category names to lists of metrics
    """
    all_metrics = await get_player_metrics(db, player_id, snapshot_id=snapshot_id)

    grouped: dict[str, list[dict]] = {}
    for metric in all_metrics:
        category = metric.get("category", "other")
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(metric)

    return grouped


async def get_similar_players(
    db: AsyncSession,
    player_id: int,
    dimension: SimilarityDimension = SimilarityDimension.composite,
    limit: int = 8,
    snapshot_id: Optional[int] = None,
) -> list[dict]:
    """
    Get similar players for comparison.

    Args:
        db: Database session
        player_id: Anchor player's ID
        dimension: Similarity dimension (anthro, combine, composite)
        limit: Maximum number of similar players to return
        snapshot_id: Optional snapshot ID (defaults to current)

    Returns:
        List of similar player dictionaries with similarity scores
    """
    # Get snapshot ID if not provided
    if snapshot_id is None:
        snapshot = await get_current_snapshot(db)
        if not snapshot:
            return []
        snapshot_id = snapshot.id

    query: Any = (
        select(PlayerSimilarity)
        .where(
            and_(
                PlayerSimilarity.anchor_player_id == player_id,  # type: ignore[arg-type]
                PlayerSimilarity.snapshot_id == snapshot_id,  # type: ignore[arg-type]
                PlayerSimilarity.dimension == dimension,  # type: ignore[arg-type]
            )
        )
        .order_by(PlayerSimilarity.rank_within_anchor)  # type: ignore[arg-type]
        .limit(limit)
    )

    result = await db.execute(query)
    similarities = result.scalars().all()

    return [
        {
            "comparison_player_id": s.comparison_player_id,
            "similarity_score": s.similarity_score,
            "distance": s.distance,
            "rank": s.rank_within_anchor,
            "badge_class": get_similarity_badge_class(s.similarity_score),
        }
        for s in similarities
    ]


async def get_comparison_metrics(
    db: AsyncSession,
    player_a_id: int,
    player_b_id: int,
    category: Optional[MetricCategory] = None,
    snapshot_id: Optional[int] = None,
) -> dict:
    """
    Get comparison metrics for two players.

    Args:
        db: Database session
        player_a_id: First player's ID
        player_b_id: Second player's ID
        category: Optional category filter
        snapshot_id: Optional snapshot ID (defaults to current)

    Returns:
        Dictionary with player metrics and comparison data
    """
    metrics_a = await get_player_metrics(db, player_a_id, category, snapshot_id)
    metrics_b = await get_player_metrics(db, player_b_id, category, snapshot_id)

    # Build lookup for player B metrics
    b_lookup = {m["metric_key"]: m for m in metrics_b}

    comparisons = []
    wins_a = 0
    wins_b = 0

    for metric_a in metrics_a:
        metric_key = metric_a["metric_key"]
        metric_b = b_lookup.get(metric_key)

        if not metric_b:
            continue

        val_a = metric_a.get("raw_value")
        val_b = metric_b.get("raw_value")

        # Determine winner (higher is generally better for these metrics)
        winner = None
        if val_a is not None and val_b is not None:
            if val_a > val_b:
                winner = "a"
                wins_a += 1
            elif val_b > val_a:
                winner = "b"
                wins_b += 1

        comparisons.append(
            {
                "metric_key": metric_key,
                "display_name": metric_a["display_name"],
                "unit": metric_a.get("unit"),
                "value_a": val_a,
                "value_b": val_b,
                "percentile_a": metric_a.get("percentile"),
                "percentile_b": metric_b.get("percentile"),
                "winner": winner,
            }
        )

    # Calculate similarity between players for this category
    similarity = await _get_pairwise_similarity(
        db, player_a_id, player_b_id, snapshot_id
    )

    return {
        "comparisons": comparisons,
        "wins_a": wins_a,
        "wins_b": wins_b,
        "total_metrics": len(comparisons),
        "similarity_score": similarity,
    }


async def _get_pairwise_similarity(
    db: AsyncSession,
    player_a_id: int,
    player_b_id: int,
    snapshot_id: Optional[int] = None,
) -> Optional[float]:
    """Get similarity score between two players."""
    if snapshot_id is None:
        snapshot = await get_current_snapshot(db)
        if not snapshot:
            return None
        snapshot_id = snapshot.id

    query: Any = select(PlayerSimilarity).where(
        and_(
            PlayerSimilarity.anchor_player_id == player_a_id,  # type: ignore[arg-type]
            PlayerSimilarity.comparison_player_id == player_b_id,  # type: ignore[arg-type]
            PlayerSimilarity.snapshot_id == snapshot_id,  # type: ignore[arg-type]
        )
    )

    result = await db.execute(query)
    similarity = result.scalar_one_or_none()

    return similarity.similarity_score if similarity else None


async def get_metric_definitions(
    db: AsyncSession,
    category: Optional[MetricCategory] = None,
) -> list[dict]:
    """
    Get all metric definitions.

    Args:
        db: Database session
        category: Optional category filter

    Returns:
        List of metric definition dictionaries
    """
    query: Any = select(MetricDefinition)

    if category:
        query = query.where(MetricDefinition.category == category)  # type: ignore[arg-type]

    query = query.order_by(MetricDefinition.display_name)

    result = await db.execute(query)
    definitions = result.scalars().all()

    return [
        {
            "id": d.id,
            "metric_key": d.metric_key,
            "display_name": d.display_name,
            "short_label": d.short_label,
            "category": d.category.value if d.category else None,
            "unit": d.unit,
            "description": d.description,
        }
        for d in definitions
    ]

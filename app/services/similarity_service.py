"""Service for player similarity queries."""

from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricSource, SimilarityDimension
from app.schemas.metrics import MetricSnapshot, PlayerSimilarity
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position


# Map similarity dimensions to metric sources
DIMENSION_TO_SOURCE: dict[SimilarityDimension, MetricSource] = {
    SimilarityDimension.anthro: MetricSource.combine_anthro,
    SimilarityDimension.combine: MetricSource.combine_agility,
    SimilarityDimension.shooting: MetricSource.combine_shooting,
    SimilarityDimension.composite: MetricSource.advanced_stats,
}


async def _resolve_player(
    db: AsyncSession, slug: str
) -> Tuple[int, str, Optional[int]]:
    """Resolve player ID, display name, and draft year from slug.

    Args:
        db: Database session
        slug: Player slug identifier

    Returns:
        Tuple of (player_id, display_name, draft_year)

    Raises:
        ValueError: If player not found
    """
    stmt = select(
        PlayerMaster.id,
        PlayerMaster.display_name,
        PlayerMaster.draft_year,
    ).where(PlayerMaster.slug == slug)  # type: ignore[call-overload]
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise ValueError("player_not_found")
    return row.id, row.display_name, row.draft_year


async def _select_similarity_snapshot(
    db: AsyncSession, source: MetricSource
) -> Optional[MetricSnapshot]:
    """Select the current snapshot for similarity queries, preferring global_scope.

    Args:
        db: Database session
        source: Metric source to find snapshot for

    Returns:
        MetricSnapshot if found, None otherwise
    """
    # Prefer global_scope snapshots
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


async def get_similar_players(
    db: AsyncSession,
    slug: str,
    dimension: SimilarityDimension,
    same_position: bool = False,
    same_draft_year: bool = False,
    nba_only: bool = False,
    limit: int = 10,
) -> dict:
    """Fetch similar players for a given player and dimension.

    Args:
        db: Database session
        slug: Player slug (e.g., "cooper-flagg")
        dimension: Similarity dimension (anthro, combine, shooting, composite)
        same_position: Filter to players with shared_position=True
        same_draft_year: Filter to players with same draft_year as anchor
        nba_only: Filter to players with is_active_nba=True
        limit: Maximum number of results

    Returns:
        Dict matching PlayerSimilarityResponse schema

    Raises:
        ValueError: If player not found
    """
    # Resolve anchor player
    anchor_id, _anchor_name, anchor_draft_year = await _resolve_player(db, slug)

    # Select snapshot
    source = DIMENSION_TO_SOURCE.get(dimension)
    if source is None:
        return {
            "anchor_slug": slug,
            "dimension": dimension,
            "snapshot_id": None,
            "players": [],
        }

    snapshot = await _select_similarity_snapshot(db, source)
    if not snapshot:
        return {
            "anchor_slug": slug,
            "dimension": dimension,
            "snapshot_id": None,
            "players": [],
        }

    # Build query
    stmt = (
        select(  # type: ignore[call-overload, misc]
            PlayerSimilarity.similarity_score,
            PlayerSimilarity.rank_within_anchor,
            PlayerSimilarity.shared_position,
            PlayerMaster.id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            PlayerMaster.school,
            PlayerMaster.draft_year,
            Position.code.label("position_code"),  # type: ignore[attr-defined]
        )
        .select_from(PlayerSimilarity)
        .join(
            PlayerMaster,
            PlayerSimilarity.comparison_player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .outerjoin(
            PlayerStatus,
            PlayerStatus.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .outerjoin(
            Position,
            Position.id == PlayerStatus.position_id,  # type: ignore[arg-type]
        )
        .where(PlayerSimilarity.anchor_player_id == anchor_id)  # type: ignore[arg-type]
        .where(PlayerSimilarity.snapshot_id == snapshot.id)  # type: ignore[arg-type]
        .where(PlayerSimilarity.dimension == dimension)  # type: ignore[arg-type]
    )

    # Apply filters
    if same_position:
        stmt = stmt.where(PlayerSimilarity.shared_position.is_(True))  # type: ignore[union-attr]

    if same_draft_year and anchor_draft_year is not None:
        stmt = stmt.where(PlayerMaster.draft_year == anchor_draft_year)  # type: ignore[arg-type]

    if nba_only:
        stmt = stmt.where(PlayerStatus.is_active_nba.is_(True))  # type: ignore[union-attr]

    # Order and limit
    stmt = stmt.order_by(
        PlayerSimilarity.rank_within_anchor.asc()  # type: ignore[union-attr]
    ).limit(limit)

    # Execute
    result = await db.execute(stmt)
    rows = result.all()

    # Format response
    players = [
        {
            "id": row.id,
            "slug": row.slug,
            "display_name": row.display_name,
            "position": row.position_code,
            "school": row.school,
            "draft_year": row.draft_year,
            "similarity_score": row.similarity_score,
            "rank": row.rank_within_anchor,
            "shared_position": row.shared_position or False,
        }
        for row in rows
    ]

    return {
        "anchor_slug": slug,
        "dimension": dimension,
        "snapshot_id": snapshot.id,
        "players": players,
    }

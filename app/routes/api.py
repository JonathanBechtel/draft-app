"""
API routes module.

JSON API endpoints for frontend AJAX calls and external integrations.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.db_async import get_session
from app.services import player as player_service
from app.services import metrics as metrics_service

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/players/search")
async def search_players(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    db: AsyncSession = Depends(get_session),
):
    """
    Search players by name for autocomplete.

    Args:
        q: Search query string
        limit: Maximum number of results

    Returns:
        List of matching player summaries
    """
    results = await player_service.search_players(db, q, limit=limit)
    return {"results": results, "count": len(results)}


@router.get("/players/{player_id}")
async def get_player(
    player_id: int,
    db: AsyncSession = Depends(get_session),
):
    """
    Get player details by ID.

    Args:
        player_id: Player's unique identifier

    Returns:
        Player detail dictionary
    """
    player = await player_service.get_player_detail(db, player_id)

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    return player


@router.get("/players/{player_id}/metrics")
async def get_player_metrics(
    player_id: int,
    category: Optional[str] = Query(None, description="Metric category filter"),
    db: AsyncSession = Depends(get_session),
):
    """
    Get metrics for a player.

    Args:
        player_id: Player's unique identifier
        category: Optional category filter

    Returns:
        List of player metrics
    """
    from app.models.fields import MetricCategory

    cat = None
    if category:
        try:
            cat = MetricCategory(category)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid category. Valid values: {[c.value for c in MetricCategory]}",
            )

    metrics = await metrics_service.get_player_metrics(db, player_id, category=cat)
    return {"player_id": player_id, "metrics": metrics, "count": len(metrics)}


@router.get("/players/{player_id}/similar")
async def get_similar_players(
    player_id: int,
    dimension: str = Query("composite", description="Similarity dimension"),
    limit: int = Query(8, ge=1, le=20, description="Max results"),
    db: AsyncSession = Depends(get_session),
):
    """
    Get similar players for comparison.

    Args:
        player_id: Anchor player's ID
        dimension: Similarity dimension (anthro, combine, composite)
        limit: Maximum number of similar players

    Returns:
        List of similar players with scores
    """
    from app.models.fields import SimilarityDimension

    try:
        dim = SimilarityDimension(dimension)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid dimension. Valid values: {[d.value for d in SimilarityDimension]}",
        )

    similar = await metrics_service.get_similar_players(
        db, player_id, dimension=dim, limit=limit
    )
    return {"player_id": player_id, "similar_players": similar, "count": len(similar)}


@router.get("/players/compare")
async def compare_players(
    player_a: int = Query(..., description="First player ID"),
    player_b: int = Query(..., description="Second player ID"),
    category: Optional[str] = Query(None, description="Metric category filter"),
    db: AsyncSession = Depends(get_session),
):
    """
    Compare two players head-to-head.

    Args:
        player_a: First player's ID
        player_b: Second player's ID
        category: Optional category filter

    Returns:
        Comparison data with metrics for both players
    """
    from app.models.fields import MetricCategory

    # Validate both players exist
    player_a_data = await player_service.get_player_detail(db, player_a)
    player_b_data = await player_service.get_player_detail(db, player_b)

    if not player_a_data:
        raise HTTPException(status_code=404, detail=f"Player {player_a} not found")
    if not player_b_data:
        raise HTTPException(status_code=404, detail=f"Player {player_b} not found")

    cat = None
    if category:
        try:
            cat = MetricCategory(category)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid category. Valid values: {[c.value for c in MetricCategory]}",
            )

    comparison = await metrics_service.get_comparison_metrics(
        db, player_a, player_b, category=cat
    )

    return {
        "player_a": {
            "id": player_a,
            "name": player_a_data["display_name"],
        },
        "player_b": {
            "id": player_b,
            "name": player_b_data["display_name"],
        },
        **comparison,
    }


@router.get("/prospects")
async def get_prospects(
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    draft_year: Optional[int] = Query(None, description="Filter by draft year"),
    db: AsyncSession = Depends(get_session),
):
    """
    Get top prospects list.

    Args:
        limit: Maximum number of prospects
        draft_year: Optional draft year filter

    Returns:
        List of top prospect summaries
    """
    prospects = await player_service.get_top_prospects(
        db, limit=limit, draft_year=draft_year
    )
    return {"prospects": prospects, "count": len(prospects)}

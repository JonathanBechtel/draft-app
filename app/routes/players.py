from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricCategory
from app.models.head_to_head import HeadToHeadResponse
from app.models.metrics import PlayerMetricsResponse
from app.models.players import PlayerSearchResult
from app.services.head_to_head_service import get_head_to_head_comparison
from app.services.metrics_service import get_player_metrics
from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session

router = APIRouter(tags=["players"])


@router.get("/players", response_model=List[PlayerSearchResult])
async def list_players(
    db: AsyncSession = Depends(get_session),
) -> List[PlayerSearchResult]:
    """List all players (using players_master)."""
    result = await db.execute(
        select(
            PlayerMaster.id,
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,
        ).order_by(PlayerMaster.display_name)  # type: ignore[call-overload]
    )
    return [
        PlayerSearchResult(
            id=row.id,
            display_name=row.display_name,
            slug=row.slug,
            school=row.school,
        )
        for row in result.all()
    ]


@router.get("/players/search", response_model=List[PlayerSearchResult])
async def search_players(
    q: str = Query(..., min_length=1, description="Search query"),
    db: AsyncSession = Depends(get_session),
) -> List[PlayerSearchResult]:
    """Search players by name (typeahead)."""
    search_pattern = f"%{q}%"
    query = (
        select(
            PlayerMaster.id,
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,
        )  # type: ignore[call-overload]
        .where(
            PlayerMaster.display_name.ilike(search_pattern)  # type: ignore[union-attr]
        )
        .order_by(PlayerMaster.display_name)
        .limit(10)
    )
    result = await db.execute(query)
    rows = result.all()

    return [
        PlayerSearchResult(
            id=row.id,
            display_name=row.display_name,
            slug=row.slug,
            school=row.school,
        )
        for row in rows
    ]


@router.get(
    "/api/players/{slug}/metrics",
    response_model=PlayerMetricsResponse,
    tags=["players"],
)
async def get_player_metrics_handler(
    slug: str,
    cohort: CohortType,
    category: MetricCategory,
    position_adjusted: bool = True,
    season_id: int | None = None,
    db: AsyncSession = Depends(get_session),
) -> PlayerMetricsResponse:
    """Return percentile metrics for a player and cohort/category combination."""
    try:
        result = await get_player_metrics(
            db=db,
            slug=slug,
            cohort=cohort,
            category=category,
            position_adjusted=position_adjusted,
            season_id=season_id,
        )
    except ValueError as exc:
        if str(exc) == "player_not_found":
            raise HTTPException(status_code=404, detail="Player not found") from exc
        raise

    return PlayerMetricsResponse(**result)


@router.get(
    "/api/players/head-to-head",
    response_model=HeadToHeadResponse,
    tags=["players"],
)
async def head_to_head_comparison(
    player_a: str = Query(..., description="Slug for player A"),
    player_b: str = Query(..., description="Slug for player B"),
    category: MetricCategory = Query(..., description="Metric category to compare"),
    db: AsyncSession = Depends(get_session),
) -> HeadToHeadResponse:
    """Return head-to-head comparison metrics for two players."""
    try:
        result = await get_head_to_head_comparison(
            db=db,
            player_a_slug=player_a,
            player_b_slug=player_b,
            category=category,
        )
    except ValueError as exc:
        if str(exc) == "player_not_found":
            raise HTTPException(status_code=404, detail="Player not found") from exc
        raise

    return HeadToHeadResponse(**result)

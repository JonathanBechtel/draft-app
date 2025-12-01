from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.players import PlayerSearchResult
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

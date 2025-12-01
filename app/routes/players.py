from typing import List
from fastapi import APIRouter, Depends, Query

from app.models.players import PlayerSearchResult
from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

router = APIRouter(tags=["players"])


@router.get("/players/search", response_model=List[PlayerSearchResult])
async def search_players(
    q: str = Query(..., min_length=1, description="Search query"),
    db: AsyncSession = Depends(get_session),
):
    """Search players by name (typeahead).

    Args:
        q: Search query (case-insensitive partial match on display_name)

    Returns:
        List of matching players with id, display_name, slug, and school
    """
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

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query

from app.models.players import PlayerCreate, PlayerRead, PlayerSearchResult
from app.schemas.players import PlayerTable
from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

router = APIRouter(tags=["players"])


@router.delete("/players/{player_id}", status_code=204)
async def delete_player(player_id: int, db: AsyncSession = Depends(get_session)):
    """Delete a player from the application"""

    async with db.begin():
        player = await db.get(PlayerTable, player_id)
        if not player:
            raise HTTPException(status_code=404, detail="Player not found")
        await db.delete(player)


# TO LEARN:  nuance of when / why to include the status code
@router.post("/players", response_model=PlayerRead, status_code=201)
async def create_player(player: PlayerCreate, db: AsyncSession = Depends(get_session)):
    """Post request to store a players information"""
    db_player = PlayerTable(**player.model_dump())

    async with db.begin():
        db.add(db_player)

    return db_player


@router.get("/players", response_model=List[PlayerRead])
async def list_players(db: AsyncSession = Depends(get_session)):
    """List all players from the database"""
    results = await db.execute(select(PlayerTable).order_by(PlayerTable.name))
    return results.scalars().all()


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
        )  # type: ignore[arg-type]
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

from typing import List
from fastapi import APIRouter, Depends, HTTPException

from app.models.players import PlayerCreate, PlayerRead
from app.schemas.players import PlayerTable
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

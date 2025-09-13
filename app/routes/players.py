import logging

from typing import List
from fastapi import APIRouter, Depends, HTTPException

from app.models.players import Player, PlayerCreate, PlayerRead
from app.utils.db_async import get_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(tags=['players'])

@router.delete("/players/{player_id}", status_code = 204)
async def delete_player(player_id: int, 
                        db: AsyncSession = Depends(get_session)):
    """Delete a player from the application"""
    player = await db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    async with db.begin():
        await db.delete(player)

# TO LEARN:  nuance of when / why to include the status code
@router.post("/players", response_model = PlayerRead, status_code = 201)
async def create_player(player: PlayerCreate, db: AsyncSession = Depends(get_session)):
    """Post request to store a players information"""
    db_player = Player(**player.model_dump())

    async with db.begin():
        db.add(db_player)

    return db_player

@router.get("/players", response_model = List[PlayerRead])
async def list_players(db: AsyncSession = Depends(get_session)):
    """List all players from the database"""
    results = await db.execute(select(Player).order_by(Player.name))
    return results.scalars().all()
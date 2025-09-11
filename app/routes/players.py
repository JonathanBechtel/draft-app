from typing import List
from fastapi import APIRouter
from app.models.players import Player, PlayerCreate, PlayerRead

router = APIRouter(prefix="/players", tags=['players'])

@router.delete("/players/{player_id}")
async def delete_player(player_id):
    """Delete a player from the application"""
    pass

# TO LEARN:  nuance of when / why to include the status code
@router.post("/players/", response_model = Player, status_code = 201)
async def create_player(player: PlayerCreate):
    """Post request to store a players information"""
    db_player = Player(player.model_dump())

    # TO DO:  ADD DB STUFF HERE

    return db_player

@router.get("/players", response_model = List[PlayerRead])
async def list_players():
    """List all players from the database"""
    return None
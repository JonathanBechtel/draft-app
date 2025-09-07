"""
Main entry point for FastAPI application.
"""

from fastapi import FastAPI
from enum import Enum
from datetime import date
from pydantic import BaseModel, Field
from typing import Annotated

class Position(str, Enum):
    f = "forward"
    c = "center"
    g = "guard"

birth_year = Annotated[date, Field(..., ge=date(1980, 1, 1))]

class Player(BaseModel):
    name: str
    position: Position
    school: str
    age: int
    birth_year: birth_year

app = FastAPI()

@app.delete("/players/{player_id}"):
async def delete_player(player_id):
    """Delete a player from the application"""
    pass

@app.post("/players/")
async def create_player(player: Player):
    """Post request to store a players information"""
    return player

@app.get("/players"):
async def list_players():
    """List all players from the database"""
    pass

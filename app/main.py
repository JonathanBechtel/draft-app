"""
Main entry point for FastAPI application.
"""
from fastapi import FastAPI
from enum import Enum
from datetime import date
from pydantic import BaseModel

class Position(str, Enum):
    f = "forward"
    c = "center"
    g = "guard"

class Player(BaseModel):
    name: str
    position: Position
    school: str
    age: int
    birth_year: date

app = FastAPI()

# notes
# first arg in function automatically maps to path parameter, names must match

@app.get("/players/{player_id}")
async def find_player(player_id: int, position: Position = "guard"):
    return {"player_id": player_id,
            "position": position,
            "name": "player name"}

@app.post("/players/")
async def create_player(player: Player):
    # does just this encapsulate all the necessary logic?  
    # anything I'm missing?
    return player
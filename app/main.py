"""
Main entry point for FastAPI application.
"""
from fastapi import FastAPI

app = FastAPI()

@app.get("/players/{player_id}")
async def find_player(player_id: int):
    return {"player_id": player_id,
            "name": "player name"}
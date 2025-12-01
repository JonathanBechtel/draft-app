# Route Patterns and Transaction Examples

Small, focused patterns to illustrate how we structure FastAPI routes, wire the async session, and wrap writes in transactions. These mirror the demo `players` endpoints and can be reused for new resources.

## CRUD example (async, transactional)

```python
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.players import PlayerCreate, PlayerRead, PlayerSearchResult
from app.schemas.players import PlayerTable
from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session

router = APIRouter(tags=["players"])


@router.post("/players", response_model=PlayerRead, status_code=201)
async def create_player(player: PlayerCreate, db: AsyncSession = Depends(get_session)):
    """Create a player row using an explicit transaction."""
    db_player = PlayerTable(**player.model_dump())
    async with db.begin():
        db.add(db_player)
    return db_player


@router.get("/players", response_model=List[PlayerRead])
async def list_players(db: AsyncSession = Depends(get_session)):
    """Read-only route—no transaction block required."""
    result = await db.execute(select(PlayerTable).order_by(PlayerTable.name))
    return result.scalars().all()


@router.delete("/players/{player_id}", status_code=204)
async def delete_player(player_id: int, db: AsyncSession = Depends(get_session)):
    """Delete with 404 on missing row and transactional safety."""
    async with db.begin():
        player = await db.get(PlayerTable, player_id)
        if not player:
            raise HTTPException(status_code=404, detail="Player not found")
        await db.delete(player)


@router.get("/players/search", response_model=List[PlayerSearchResult])
async def search_players(
    q: str = Query(..., min_length=1, description="Search query"),
    db: AsyncSession = Depends(get_session),
):
    """Typeahead search against players_master for richer identity data."""
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
```

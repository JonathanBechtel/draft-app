"""Public API routes for retained Summer League metric trends."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.summer_league_trends import TrendPoint
from app.services.sources.summer_league.metric_trends import get_daily_trend
from app.utils.db_async import get_session

router = APIRouter(tags=["summer-league"])


@router.get(
    "/api/summer-league/trends",
    response_model=list[TrendPoint],
    status_code=200,
)
async def summer_league_daily_trends(
    scope_key: str = Query(..., min_length=1),
    player_id: int | None = Query(default=None, ge=1),
    metric_keys: list[str] = Query(default=["gmsc", "ts_pct", "bpm"]),
    db: AsyncSession = Depends(get_session),
) -> list[TrendPoint]:
    """Return ordered daily-close trend points for a stable event scope."""
    try:
        return await get_daily_trend(
            db,
            scope_key=scope_key,
            player_id=player_id,
            metric_keys=metric_keys,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

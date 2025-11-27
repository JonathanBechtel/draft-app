"""
UI routes module.

HTML page routes using Jinja2 templates.
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.db_async import get_session
from app.services import player as player_service
from app.services import metrics as metrics_service

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """
    Render the Homepage.

    Displays:
    - Market Moves ticker (feature-flagged)
    - Consensus Mock Draft table (feature-flagged)
    - Top Prospects grid (feature-flagged)
    - VS Arena comparison tool (feature-flagged)
    - Live Draft Buzz feed (feature-flagged)
    - Draft Position Specials (feature-flagged, off by default)
    """
    # Get top prospects for the grid
    prospects = []
    if settings.FEATURE_TOP_PROSPECTS:
        prospects = await player_service.get_top_prospects(db, limit=6)

    # Get players for comparison dropdowns
    comparison_players = []
    if settings.FEATURE_VS_ARENA:
        comparison_players = await player_service.get_players_for_comparison(
            db, limit=50
        )

    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "settings": settings,
            "prospects": prospects,
            "comparison_players": comparison_players,
            # Placeholder data for features not yet fully implemented
            "market_moves": [],
            "mock_draft": [],
            "news_items": [],
            "specials": [],
        },
    )


@router.get("/player/{player_id}", response_class=HTMLResponse)
async def player_detail(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
):
    """
    Render the Player Detail Page.

    Displays:
    - Player bio and photo
    - Analytics dashboard/scoreboard
    - Performance percentile bars
    - Similar player comparisons
    - Head-to-head comparison tool
    - Player-specific news
    """
    # Get player detail
    player = await player_service.get_player_detail(db, player_id)

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Get metrics grouped by category
    metrics_by_category = {}
    if settings.FEATURE_PLAYER_PERCENTILES:
        metrics_by_category = await metrics_service.get_metrics_by_category(
            db, player_id
        )

    # Get similar players
    similar_players = []
    if settings.FEATURE_PLAYER_COMPARISONS:
        similar_players = await metrics_service.get_similar_players(
            db, player_id, limit=8
        )

    # Get players for H2H comparison
    comparison_players = []
    if settings.FEATURE_PLAYER_H2H:
        comparison_players = await player_service.get_players_for_comparison(
            db, limit=50
        )

    return request.app.state.templates.TemplateResponse(
        "player_detail.html",
        {
            "request": request,
            "settings": settings,
            "player": player,
            "metrics_by_category": metrics_by_category,
            "similar_players": similar_players,
            "comparison_players": comparison_players,
            # Placeholder for player news
            "news_items": [],
        },
    )


@router.get("/compare", response_class=HTMLResponse)
async def compare_players(
    request: Request,
    player_a: int | None = None,
    player_b: int | None = None,
    db: AsyncSession = Depends(get_session),
):
    """
    Render the Player Comparison Page.

    Optional dedicated page for head-to-head comparisons.
    """
    # Get players for comparison dropdowns
    comparison_players = await player_service.get_players_for_comparison(db, limit=50)

    # Get player details if IDs provided
    player_a_data = None
    player_b_data = None
    comparison_data = None

    if player_a:
        player_a_data = await player_service.get_player_detail(db, player_a)

    if player_b:
        player_b_data = await player_service.get_player_detail(db, player_b)

    if player_a and player_b:
        comparison_data = await metrics_service.get_comparison_metrics(
            db, player_a, player_b
        )

    return request.app.state.templates.TemplateResponse(
        "compare.html",
        {
            "request": request,
            "settings": settings,
            "comparison_players": comparison_players,
            "player_a": player_a_data,
            "player_b": player_b_data,
            "comparison": comparison_data,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_results(
    request: Request,
    q: str = "",
    db: AsyncSession = Depends(get_session),
):
    """
    Render the Search Results Page.
    """
    results = []
    if q:
        results = await player_service.search_players(db, q, limit=20)

    return request.app.state.templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "settings": settings,
            "query": q,
            "results": results,
        },
    )

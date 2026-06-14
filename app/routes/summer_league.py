"""Public Summer League games store: index, box score, and player game logs.

See ``docs/plans/summer-league-games-index-spec.md``. These are read-only,
raw-data surfaces over the normalized Summer League tables.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.summer_league_games_service import (
    GamesPage,
    get_game_box_score,
    get_games_facets,
    get_player_game_logs,
    resolve_player_ref,
    search_games,
)
from app.services.summer_league_season_service import (
    get_season_leaders,
    get_season_overview,
    get_season_years,
)
from app.utils.db_async import get_session

LANDING_RECENT_GAMES = 8

router = APIRouter(tags=["summer-league"])

FOOTER_LINKS = [
    {"text": "Terms of Service", "url": "/terms"},
    {"text": "Privacy Policy", "url": "/privacy"},
    {"text": "Cookie Policy", "url": "/cookies"},
]


@router.get("/stats/summer-league/games", response_class=HTMLResponse)
async def summer_league_games_index(
    request: Request,
    year: int | None = Query(default=None),
    venue: str | None = Query(default=None),
    team: str | None = Query(default=None),
    player: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Centralized, filterable index of every Summer League game."""
    player_id: int | None = None
    player_label: str | None = None
    player_unresolved = False
    if player:
        ref = await resolve_player_ref(db, player)
        if ref is not None:
            player_id = ref.id
            player_label = ref.name
        else:
            # A requested player filter that can't be resolved (stale/mistyped
            # slug) must not silently fall through to "all games" — show no
            # results so the failed filter is visible.
            player_unresolved = True
            player_label = player

    if player_unresolved:
        result = GamesPage(games=[], total=0, page=1, page_size=1, total_pages=1)
    else:
        result = await search_games(
            db,
            year=year,
            venue_slug=venue,
            team_slug=team,
            player_id=player_id,
            page=page,
        )
    facets = await get_games_facets(db)

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/games.html",
        {
            "request": request,
            "result": result,
            "facets": facets,
            "active": {
                "year": year,
                "venue": venue,
                "team": team,
                "player": player,
            },
            "player_label": player_label,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league/{year}/games/{game_id}", response_class=HTMLResponse)
async def summer_league_game_box_score(
    request: Request,
    year: int,
    game_id: int,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Full box score for one Summer League game."""
    box = await get_game_box_score(db, game_id)
    if box is None:
        raise HTTPException(status_code=404, detail="Summer League game not found")

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/game-detail.html",
        {
            "request": request,
            "box": box,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/players/{slug}/summer-league", response_class=HTMLResponse)
async def player_summer_league_logs(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """A player's complete Summer League game logs grouped by competition."""
    ref = await resolve_player_ref(db, slug)
    if ref is None:
        raise HTTPException(status_code=404, detail="Player not found")

    seasons = await get_player_game_logs(db, ref.id)
    total_games = sum(len(s.rows) for s in seasons)

    return request.app.state.templates.TemplateResponse(
        "players/summer-league-logs.html",
        {
            "request": request,
            "player": {
                "id": ref.id,
                "slug": ref.slug,
                "name": ref.name,
            },
            "seasons": seasons,
            "total_games": total_games,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league", response_class=HTMLResponse)
async def summer_league_landing(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Front door for the Summer League section."""
    years = await get_season_years(db)
    latest = years[0] if years else None
    overview = await get_season_overview(db, latest) if latest is not None else None
    leaders = await get_season_leaders(db, latest) if latest is not None else None
    recent = await search_games(db, page=1, page_size=LANDING_RECENT_GAMES)

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/landing.html",
        {
            "request": request,
            "years": years,
            "latest_year": latest,
            "overview": overview,
            "leaders": leaders,
            "recent": recent,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league/{year}", response_class=HTMLResponse)
async def summer_league_season(
    request: Request,
    year: int,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Season hub: a year's venues, schedule, and leaderboards."""
    overview = await get_season_overview(db, year)
    if overview is None:
        raise HTTPException(
            status_code=404, detail="No Summer League data for this year"
        )

    years = await get_season_years(db)
    leaders = await get_season_leaders(db, year)
    schedule = await search_games(db, year=year, page=1, page_size=12)

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/season.html",
        {
            "request": request,
            "year": year,
            "years": years,
            "overview": overview,
            "leaders": leaders,
            "schedule": schedule,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )

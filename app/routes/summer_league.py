"""Public Summer League games store: index, box score, and player game logs.

See ``docs/plans/summer-league-games-index-spec.md``. These are read-only,
raw-data surfaces over the normalized Summer League tables.
"""

from __future__ import annotations

# discipline: file-size existing page family; trend computation/API live in scoped modules

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.services.summer_league_games_service import (
    GamesPage,
    get_game_box_score,
    get_game_flow_series,
    get_games_facets,
    get_player_game_logs,
    resolve_player_ref,
    search_games,
)
from app.services.summer_league_metrics_service import get_player_metric_seasons
from app.services.summer_league_shotchart_service import get_game_shotchart_scopes
from app.services.summer_league_stats_service import (
    get_competition_id_for_player_year,
    get_player_shotchart_context,
)
from app.services.summer_league_explorer_service import (
    PLAYER_COLUMN_CATALOG,
    PER_GAME_FILTERABLE_COLUMNS,
    TEAM_FILTERABLE_COLUMNS,
    competition_filterable_columns,
    get_player_drilldown_rows,
    parse_query,
    run_explorer_query,
)
from app.services.summer_league_environment_registry import metrics_for_scope
from app.schemas.summer_league_environment import (
    SCOPE_KIND_COMPETITION,
    SCOPE_KIND_SEASON,
)
from app.services.summer_league_environment_service import (
    build_profile_summary_view,
    competition_scope_key,
    get_current_profile_by_scope_key,
    season_scope_key,
)
from app.services.summer_league.metric_trends import build_trend_context
from app.services.summer_league_franchise_service import get_franchise_history
from app.services.summer_league_leaders_service import get_leaders
from app.services.summer_league_season_service import (
    get_alltime_leaders,
    get_season_leaders,
    get_season_overview,
    get_season_years,
    get_venue_leaders,
)
from app.services.summer_league_team_service import (
    get_team_season,
    get_venue,
    get_venue_bracket,
)
from app.utils.db_async import get_session

# Schedule lists on the season-hub and venue pages page 20 games at a time so
# the section stays compact instead of scrolling the whole slate.
SCHEDULE_PAGE_SIZE = 20

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


def _parse_gate(raw: str | None) -> int | None:
    """Parse a Min GP / Min MIN query value; blank or invalid means adaptive."""
    if raw is None or raw.strip() == "":
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


@router.get("/stats/summer-league/leaders", response_class=HTMLResponse)
async def summer_league_leaders(
    request: Request,
    mode: str = Query(default="per_game"),
    year: int | None = Query(default=None),
    venue: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    dir: str = Query(default="desc"),
    min_gp: str | None = Query(default=None),
    min_min: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Comprehensive, sortable Summer League leaderboard across display modes.

    ``min_gp`` / ``min_min`` left blank apply the adaptive gate ladder (standard
    thresholds, relaxed rung by rung until the board populates); explicit values
    are honored exactly.
    """
    result = await get_leaders(
        db,
        mode=mode,
        year=year,
        venue=venue,
        sort=sort,
        direction=dir,
        min_games=_parse_gate(min_gp),
        min_minutes=_parse_gate(min_min),
        page=page,
    )
    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/leaders.html",
        {
            "request": request,
            "result": result,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


def _explorer_csv(result: object) -> StreamingResponse:
    """Build a CSV StreamingResponse from an :class:`ExplorerResult`.

    The first column is the row label (Player / Team / Game name); subsequent
    columns follow ``result.columns`` in display order.  None values render as
    empty strings.
    """
    from app.services.summer_league_explorer_service import ExplorerResult

    assert isinstance(result, ExplorerResult)

    # Determine the label column header from the subject.
    label_header = {
        "players": "Player",
        "teams": "Team",
        "games": "Game",
        "competitions": "Scope",
    }.get(result.subject, "Name")

    buf = io.StringIO()
    writer = csv.writer(buf)

    # Header row: label column first, then stat columns.
    writer.writerow([label_header] + [c.label for c in result.columns])

    for row in result.rows:
        writer.writerow(
            [row.label]
            + [
                "" if row.values.get(c.key) is None else row.values.get(c.key)
                for c in result.columns
            ]
        )

    # Competition Context CSV surfaces the same invalid-parameter notes as the
    # HTML view so a downloaded export never looks cleaner than the page it
    # came from (ticket #636; contract §6).
    if result.subject == "competitions" and (
        result.competition_not_found or result.query.validation_errors
    ):
        writer.writerow([])
        writer.writerow(["# Notes"])
        if result.competition_not_found:
            writer.writerow(
                [
                    "This competition could not be found. It may have been "
                    "removed, or the link may be out of date."
                ]
            )
        for message in result.query.validation_errors:
            writer.writerow([message])

    # Competition Context CSV also ships the metric definition dictionary so the
    # export is self-describing (contract §6: values + definitions/units +
    # coverage + freshness + version, matching the HTML detail definitions).
    if result.subject == "competitions":
        scope_kind = (
            SCOPE_KIND_COMPETITION
            if result.query.profile_scope == "competition"
            else SCOPE_KIND_SEASON
        )
        writer.writerow([])
        writer.writerow(["# Metric definitions"])
        writer.writerow(
            [
                "metric_key",
                "label",
                "unit",
                "scale",
                "formula",
                "denominator",
                "required_coverage",
                "interpretation",
            ]
        )
        for definition in metrics_for_scope(scope_kind):
            writer.writerow(
                [
                    definition.key,
                    definition.label,
                    definition.unit.value,
                    definition.scale,
                    definition.formula,
                    definition.denominator,
                    definition.coverage_source.value,
                    definition.interpretation,
                ]
            )

    buf.seek(0)
    filename = (
        "summer-league-competitions.csv"
        if result.subject == "competitions"
        else "summer-league-explorer.csv"
    )
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats/summer-league/explorer/drilldown", response_class=HTMLResponse)
async def summer_league_explorer_drilldown(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Per-competition drill-down for a single career-grain player row.

    Returns an HTML partial containing ``<tr>`` rows for each competition the
    player appeared in within the filtered scope.  Consumed by the JS expand
    affordance on career-grain rows in the Explorer.

    Query params: ``player_slug`` (required) + any Explorer scope filters
    (``year_min``, ``year_max``, ``venue``, ``mode``, …).  A missing or blank
    ``player_slug`` returns HTTP 400.
    """
    params = dict(request.query_params)
    player_slug = params.get("player_slug", "").strip()
    if not player_slug:
        return HTMLResponse("", status_code=400)

    scope_q = parse_query(params)
    result = await get_player_drilldown_rows(db, player_slug, scope_q)

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/_explorer_drilldown.html",
        {
            "request": request,
            "result": result,
            "player_slug": player_slug,
        },
    )


@router.get("/stats/summer-league/explorer", response_class=HTMLResponse)
async def summer_league_explorer(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Faceted query builder over Summer League players, teams, and games.

    All state is URL-encoded so every query is shareable. ``?partial=1`` renders
    just the results table for in-place JS swaps; otherwise the full page renders.
    ``?format=csv`` streams a CSV download of the current query result.
    """
    query = parse_query(
        dict(request.query_params)
        | {"coverage": ",".join(request.query_params.getlist("coverage"))}
    )

    is_csv = request.query_params.get("format") == "csv"
    if is_csv:
        query.paginate = False

    result = await run_explorer_query(db, query)

    if is_csv:
        return _explorer_csv(result)

    template = (
        "stats/summer-league/_explorer_results.html"
        if request.query_params.get("partial")
        else "stats/summer-league/explorer.html"
    )
    if query.subject == "competitions":
        filterable_columns = competition_filterable_columns(
            SCOPE_KIND_COMPETITION
            if query.profile_scope == "competition"
            else SCOPE_KIND_SEASON
        )
    elif query.subject == "players" and query.grain == "per_game":
        filterable_columns = PER_GAME_FILTERABLE_COLUMNS
    elif query.subject == "teams":
        filterable_columns = TEAM_FILTERABLE_COLUMNS
    else:
        filterable_columns = [c for c in PLAYER_COLUMN_CATALOG if c.filterable]
    return request.app.state.templates.TemplateResponse(
        template,
        {
            "request": request,
            "result": result,
            "filterable_columns": filterable_columns,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league/teams/{team}", response_class=HTMLResponse)
async def summer_league_franchise(
    request: Request,
    team: str,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """One NBA franchise's full cross-year Summer League history."""
    franchise = await get_franchise_history(db, team)
    if franchise is None:
        raise HTTPException(status_code=404, detail="Summer League franchise not found")

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/franchise.html",
        {
            "request": request,
            "franchise": franchise,
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

    # All shot-chart scopes (whole game / each team / each player) are preloaded
    # so the browser switches between them client-side — no round-trip, no scroll
    # jump. ``None`` when the game has no shot events (template shows an empty state).
    sl_shotchart_scopes = await get_game_shotchart_scopes(db, game_id=game_id)

    sl_game_flow = await get_game_flow_series(db, game_id=game_id)

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/game-detail.html",
        {
            "request": request,
            "box": box,
            "sl_shotchart_scopes": sl_shotchart_scopes,
            "sl_game_flow": sl_game_flow,
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


@router.get("/players/{slug}/summer-league/{year}", response_class=HTMLResponse)
async def player_summer_league_season(
    request: Request,
    slug: str,
    year: int,
    venue: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Per-season Summer League view: game logs + shot chart for one year.

    ``venue`` (a competition ``venue_slug``) scopes the shot chart to the exact
    competition the user clicked; without it, the year's marquee competition is
    used.
    """
    ref = await resolve_player_ref(db, slug)
    if ref is None:
        raise HTTPException(status_code=404, detail="Player not found")

    all_seasons = await get_player_game_logs(db, ref.id)
    year_seasons = [s for s in all_seasons if s.year == year]
    total_games = sum(len(s.rows) for s in year_seasons)

    # Advanced season line(s) for this year — one per adv-eligible competition
    # (same full BBRef column set as the player-detail advanced table).
    adv_profile = await get_player_metric_seasons(db, ref.id)
    sl_adv_seasons = [
        s for s in (adv_profile.seasons if adv_profile else []) if s.year == year
    ]

    # Shot chart: scoped to the clicked competition (venue) or the marquee one.
    sl_shotchart: dict | None = None
    comp_id = await get_competition_id_for_player_year(
        db, ref.id, year, venue_slug=venue
    )
    if comp_id is not None:
        sl_shotchart = await get_player_shotchart_context(
            db, player_id=ref.id, competition_id=comp_id
        )
    sl_trend = (
        await build_trend_context(
            db,
            scope_key=f"competition:{comp_id}",
            scope_label=f"{year} trend",
            player_id=ref.id,
        )
        if comp_id is not None
        else None
    )

    return request.app.state.templates.TemplateResponse(
        "players/summer-league-season.html",
        {
            "request": request,
            "player": {
                "id": ref.id,
                "slug": ref.slug,
                "name": ref.name,
            },
            "year": year,
            "seasons": year_seasons,
            "total_games": total_games,
            "sl_adv_seasons": sl_adv_seasons,
            "sl_shotchart": sl_shotchart,
            "sl_trend": sl_trend,
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
    # Hero highlights the latest season; the leaders board spans all seasons so
    # the landing isn't a duplicate of the newest season hub.
    hero_leaders = await get_season_leaders(db, latest) if latest is not None else None
    alltime = await get_alltime_leaders(db) if latest is not None else None
    recent = await search_games(db, page=1, page_size=LANDING_RECENT_GAMES)

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/landing.html",
        {
            "request": request,
            "years": years,
            "latest_year": latest,
            "overview": overview,
            "hero_leaders": hero_leaders,
            "alltime": alltime,
            "recent": recent,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league/{year}", response_class=HTMLResponse)
async def summer_league_season(
    request: Request,
    year: int,
    page: int = Query(default=1, ge=1),
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
    schedule = await search_games(
        db, year=year, page=page, page_size=SCHEDULE_PAGE_SIZE
    )

    # All-competitions Competition Context summary (#610): one indexed current-
    # profile read, reusing the #607 read contract — never a second aggregation
    # (contract §7/§9). `None` when no profile has been published yet; the
    # template renders a neutral unavailable state rather than an error.
    season_profile_row = await get_current_profile_by_scope_key(
        db, season_scope_key(year)
    )
    season_profile = (
        build_profile_summary_view(season_profile_row)
        if season_profile_row is not None
        else None
    )
    season_trend = await build_trend_context(
        db,
        scope_key=f"season:{year}",
        scope_label=f"{year} all competitions",
    )

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/season.html",
        {
            "request": request,
            "year": year,
            "years": years,
            "overview": overview,
            "leaders": leaders,
            "schedule": schedule,
            "season_profile": season_profile,
            "sl_trend": season_trend,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league/{year}/{venue}", response_class=HTMLResponse)
async def summer_league_venue(
    request: Request,
    year: int,
    venue: str,
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Single venue within a season: standings, leaders, schedule, teams."""
    detail = await get_venue(db, year, venue)
    if detail is None:
        raise HTTPException(status_code=404, detail="Summer League venue not found")

    bracket = await get_venue_bracket(db, year, venue)
    leaders = await get_venue_leaders(db, year, venue)
    schedule = await search_games(
        db, year=year, venue_slug=venue, page=page, page_size=SCHEDULE_PAGE_SIZE
    )

    # Exact single-competition Competition Context module (#610): resolved by
    # `detail.competition_id` — the canonical id `get_venue` already looked up
    # from (year, venue_slug) — never by venue label alone, so two editions
    # that happen to share a label/venue_slug across years can never leak into
    # each other's profile. One indexed current-profile read (contract §9).
    venue_profile_row = await get_current_profile_by_scope_key(
        db, competition_scope_key(detail.competition_id)
    )
    venue_profile = (
        build_profile_summary_view(venue_profile_row)
        if venue_profile_row is not None
        else None
    )
    venue_trend = await build_trend_context(
        db,
        scope_key=f"competition:{detail.competition_id}",
        scope_label=f"{detail.venue} {year}",
    )

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/venue.html",
        {
            "request": request,
            "detail": detail,
            "bracket": bracket,
            "leaders": leaders,
            "schedule": schedule,
            "venue_profile": venue_profile,
            "sl_trend": venue_trend,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/stats/summer-league/{year}/{venue}/{team}", response_class=HTMLResponse)
async def summer_league_team(
    request: Request,
    year: int,
    venue: str,
    team: str,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """One team's run at a single venue-year: record, roster, schedule."""
    team_season = await get_team_season(db, year, venue, team)
    if team_season is None:
        raise HTTPException(status_code=404, detail="Summer League team not found")

    return request.app.state.templates.TemplateResponse(
        "stats/summer-league/team.html",
        {
            "request": request,
            "team": team_season,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )

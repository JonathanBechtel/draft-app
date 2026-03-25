"""Stats routes for combine leaderboards and metric exploration."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.combine_stats_service import (
    ATHLETIC_KEYS,
    HOMEPAGE_METRIC_DISPLAY,
    MEASUREMENT_KEYS,
    SHOOTING_DRILL_DISPLAY,
    SHOOTING_KEYS,
    get_available_positions,
    get_available_years,
    get_draft_year_data,
    get_homepage_data,
    get_leaderboard,
    get_metric_info,
    get_metrics_grouped,
)
from app.utils.db_async import get_session
from app.utils.images import get_placeholder_url, get_player_image_url

router = APIRouter(prefix="/stats", tags=["stats"])

LEADERBOARD_PAGE_LIMIT = 25

FOOTER_LINKS = [
    {"text": "Terms of Service", "url": "/terms"},
    {"text": "Privacy Policy", "url": "/privacy"},
    {"text": "Cookie Policy", "url": "/cookies"},
]


def _player_photo_urls(
    player_id: int | None,
    slug: str | None,
    display_name: str | None,
) -> dict[str, str]:
    """Build photo URL dict with fallback chain for a player."""
    if not player_id or not slug:
        placeholder = get_placeholder_url(
            display_name or "Player", width=144, height=192
        )
        return {
            "photo_url": placeholder,
            "photo_url_default": placeholder,
            "photo_url_placeholder": placeholder,
        }
    return {
        "photo_url": get_player_image_url(
            player_id=player_id, slug=slug, style="default"
        ),
        "photo_url_default": get_player_image_url(
            player_id=player_id, slug=slug, style="default"
        ),
        "photo_url_placeholder": get_placeholder_url(
            display_name or "Player",
            player_id=player_id,
            width=144,
            height=192,
        ),
    }


def _entry_to_dict(entry: object) -> dict:
    """Convert a LeaderboardEntry dataclass to a template-friendly dict."""
    from app.services.combine_stats_service import LeaderboardEntry

    assert isinstance(entry, LeaderboardEntry)
    d = {
        "rank": entry.rank,
        "player_id": entry.player_id,
        "display_name": entry.display_name,
        "slug": entry.slug,
        "school": entry.school,
        "position": entry.position,
        "draft_year": entry.draft_year,
        "draft_round": entry.draft_round,
        "draft_pick": entry.draft_pick,
        "is_active_nba": entry.is_active_nba,
        "raw_value": entry.raw_value,
        "formatted_value": entry.formatted_value,
        "percentile": entry.percentile,
    }
    d.update(_player_photo_urls(entry.player_id, entry.slug, entry.display_name))
    return d


DEFAULT_METRIC = "wingspan_in"


def _shooting_entry_to_dict(entry: object) -> dict:
    """Convert a ShootingLeaderEntry dataclass to a template-friendly dict."""
    from app.services.combine_stats_service import ShootingLeaderEntry

    assert isinstance(entry, ShootingLeaderEntry)
    d = {
        "rank": entry.rank,
        "player_id": entry.player_id,
        "display_name": entry.display_name,
        "slug": entry.slug,
        "school": entry.school,
        "position": entry.position,
        "draft_year": entry.draft_year,
        "fgm": entry.fgm,
        "fga": entry.fga,
        "fg_pct": entry.fg_pct,
        "formatted_value": entry.formatted_value,
        "formatted_pct": entry.formatted_pct,
    }
    d.update(_player_photo_urls(entry.player_id, entry.slug, entry.display_name))
    return d


@router.get("/", response_class=HTMLResponse)
async def stats_homepage(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Stats homepage with leader cards for all combine metrics."""
    data = await get_homepage_data(db)

    measurement_leaders = {
        key: [_entry_to_dict(e) for e in entries]
        for key, entries in data.measurement_leaders.items()
    }
    athletic_leaders = {
        key: [_entry_to_dict(e) for e in entries]
        for key, entries in data.athletic_leaders.items()
    }
    shooting_leaders = {
        key: [_shooting_entry_to_dict(e) for e in entries]
        for key, entries in data.shooting_leaders.items()
    }

    return request.app.state.templates.TemplateResponse(
        "stats/index.html",
        {
            "request": request,
            "measurement_leaders": measurement_leaders,
            "athletic_leaders": athletic_leaders,
            "shooting_leaders": shooting_leaders,
            "measurement_keys": MEASUREMENT_KEYS,
            "athletic_keys": ATHLETIC_KEYS,
            "shooting_keys": SHOOTING_KEYS,
            "metric_display": HOMEPAGE_METRIC_DISPLAY,
            "shooting_display": SHOOTING_DRILL_DISPLAY,
            "year_stats": data.year_stats,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/combine/{year}", response_class=HTMLResponse)
async def draft_year_page(
    request: Request,
    year: int,
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Draft year combine stats page with category tabs and range charts."""
    import json as json_mod

    from app.services.combine_stats_service import (
        CategoryYearData,
        PlayerMetricRow,
    )

    data = await get_draft_year_data(db, year)

    if not data.available_years or year not in data.available_years:
        raise HTTPException(status_code=404, detail="No combine data for this year")

    def _build_metrics_list(metric_keys: list[str]) -> list[dict]:
        result = []
        for mk in metric_keys:
            mi = get_metric_info(mk)
            if mi:
                result.append(
                    {
                        "key": mk,
                        "label": mi.display_name,
                        "unit": mi.unit,
                        "sort_direction": mi.sort_direction,
                    }
                )
            else:
                result.append(
                    {"key": mk, "label": mk, "unit": None, "sort_direction": "desc"}
                )
        return result

    def _player_row_to_dict(pr: PlayerMetricRow) -> dict:
        d = {
            "player_id": pr.player_id,
            "display_name": pr.display_name,
            "slug": pr.slug,
            "school": pr.school,
            "position": pr.position,
            "metrics": pr.metrics,
            "formatted": pr.formatted_metrics,
            "percentiles": pr.percentiles,
        }
        d.update(_player_photo_urls(pr.player_id, pr.slug, pr.display_name))
        return d

    def _category_to_dict(cat: CategoryYearData) -> dict:
        return {
            "range_stats": [
                {
                    "metric_key": rs.metric_key,
                    "display_name": rs.display_name,
                    "unit": rs.unit,
                    "sort_direction": rs.sort_direction,
                    "min_value": rs.min_value,
                    "min_player_name": rs.min_player_name,
                    "min_player_slug": rs.min_player_slug,
                    "max_value": rs.max_value,
                    "max_player_name": rs.max_player_name,
                    "max_player_slug": rs.max_player_slug,
                    "avg_value": rs.avg_value,
                    "formatted_min": rs.formatted_min,
                    "formatted_max": rs.formatted_max,
                    "formatted_avg": rs.formatted_avg,
                }
                for rs in cat.range_stats
            ],
            "leaders": {mk: _player_row_to_dict(pr) for mk, pr in cat.leaders.items()},
            "players": [_player_row_to_dict(pr) for pr in cat.players],
            "metric_keys": cat.metric_keys,
            "metrics": _build_metrics_list(cat.metric_keys),
        }

    draft_year_json = json_mod.dumps(
        {
            "year": data.year,
            "available_years": data.available_years,
            "positions": data.positions,
            "categories": {
                "anthro": _category_to_dict(data.anthro),
                "athletic": _category_to_dict(data.athletic),
                "shooting": _category_to_dict(data.shooting),
            },
        }
    )

    return request.app.state.templates.TemplateResponse(
        "stats/draft_year.html",
        {
            "request": request,
            "year": data.year,
            "available_years": data.available_years,
            "draft_year_json": draft_year_json,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/{metric_key}", response_class=HTMLResponse)
async def metric_leaderboard(
    request: Request,
    metric_key: str,
    year: str | None = Query(default=None),
    position: str | None = Query(default=None),
    nba_status: str | None = Query(default=None),
    offset: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Metric leaderboard page with filters and summary cards."""
    try:
        years_val = [int(y) for y in year.split(",") if y.strip()] if year else None
    except ValueError:
        years_val = None
    positions_val = (
        [p.strip() for p in position.split(",") if p.strip()] if position else None
    )
    nba_status_val: bool | None = None
    if nba_status == "active":
        nba_status_val = True
    elif nba_status == "inactive":
        nba_status_val = False
    try:
        offset_val = max(0, int(offset)) if offset else 0
    except ValueError:
        offset_val = 0

    metric = get_metric_info(metric_key)
    if not metric:
        raise HTTPException(status_code=404, detail="Unknown metric")

    result = await get_leaderboard(
        db,
        metric_key,
        years=years_val,
        positions=positions_val,
        is_active_nba=nba_status_val,
        limit=LEADERBOARD_PAGE_LIMIT,
        offset=offset_val,
    )
    years = await get_available_years(db, metric_key=metric_key)
    all_positions = await get_available_positions(db, metric_key=metric_key)
    # Build display labels: "c" → "C", "pf_c" → "PF/C"
    positions = [(code, code.upper().replace("_", "/")) for code, _ in all_positions]
    metrics_grouped = get_metrics_grouped()

    entries = [_entry_to_dict(e) for e in result.entries]

    best = _entry_to_dict(result.best) if result.best else None
    worst = _entry_to_dict(result.worst) if result.worst else None
    typical = _entry_to_dict(result.typical) if result.typical else None

    # Formatted median for card header
    median_formatted = result.typical.formatted_value if result.typical else None

    return request.app.state.templates.TemplateResponse(
        "stats/metric.html",
        {
            "request": request,
            "metric": {
                "key": metric.key,
                "display_name": metric.display_name,
                "unit": metric.unit,
                "category": metric.category,
                "sort_direction": metric.sort_direction,
            },
            "entries": entries,
            "total": result.total,
            "limit": LEADERBOARD_PAGE_LIMIT,
            "offset": offset_val,
            "best": best,
            "worst": worst,
            "typical": typical,
            "median_formatted": median_formatted,
            "years": years,
            "positions": positions,
            "metrics_grouped": metrics_grouped,
            "active_years": years_val or [],
            "active_positions": positions_val or [],
            "active_nba_status": nba_status or "",
            "active_metric_key": metric_key,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )

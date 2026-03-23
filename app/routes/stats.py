"""Stats routes for combine leaderboards and metric exploration."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.combine_stats_service import (
    get_available_positions,
    get_available_years,
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


@router.get("/", response_class=RedirectResponse)
async def stats_landing() -> RedirectResponse:
    """Redirect /stats to the default metric leaderboard."""
    return RedirectResponse(url=f"/stats/{DEFAULT_METRIC}", status_code=302)


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
    years_val = [int(y) for y in year.split(",") if y.strip()] if year else None
    positions_val = (
        [p.strip() for p in position.split(",") if p.strip()] if position else None
    )
    nba_status_val: bool | None = None
    if nba_status == "active":
        nba_status_val = True
    elif nba_status == "inactive":
        nba_status_val = False
    offset_val = int(offset) if offset else 0

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

    highest = _entry_to_dict(result.highest) if result.highest else None
    lowest = _entry_to_dict(result.lowest) if result.lowest else None
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
            "highest": highest,
            "lowest": lowest,
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

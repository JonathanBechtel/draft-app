"""UI Routes - Renders Jinja templates for the frontend."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.image_assets_service import get_current_image_url_for_player
from app.services.image_assets_service import get_current_image_urls_for_players
from app.services.news_service import (
    get_author_counts,
    get_hero_article,
    get_news_feed,
    get_source_counts,
)
from app.services.player_service import get_player_profile_by_slug
from app.utils.db_async import get_session
from app.utils.images import get_placeholder_url, get_s3_image_base_url

router = APIRouter()

# Footer links - shared across all pages
FOOTER_LINKS = [
    {"text": "Terms of Service", "url": "/terms"},
    {"text": "Privacy Policy", "url": "/privacy"},
    {"text": "Cookie Policy", "url": "/cookies"},
]


# Curated list of top prospects to feature on homepage
TOP_PROSPECT_SLUGS = [
    "cooper-flagg",
    "ace-bailey",
    "dylan-harper",
    "vj-edgecombe",
    "kon-knueppel",
]


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    style: Optional[str] = Query(
        None, description="Image style: default, vector, comic, retro"
    ),
    db: AsyncSession = Depends(get_session),
):
    """Render the Homepage with prospects, VS arena, and news feed."""
    from sqlalchemy import select

    from app.schemas.players_master import PlayerMaster

    # Fetch curated top prospects from database by slug
    result = await db.execute(
        select(PlayerMaster).where(
            PlayerMaster.slug.in_(TOP_PROSPECT_SLUGS)  # type: ignore[union-attr]
        )
    )
    db_players_unordered = {p.slug: p for p in result.scalars().all()}
    # Preserve the curated order
    db_players = [
        db_players_unordered[slug]
        for slug in TOP_PROSPECT_SLUGS
        if slug in db_players_unordered
    ]

    # Build player ID map for image URL generation and JS
    player_id_map = {p.slug: (p.id, p.display_name) for p in db_players}

    requested_style = style or "default"
    player_ids = [p.id for p in db_players if p.id is not None]
    image_urls_by_id = await get_current_image_urls_for_players(
        db,
        player_ids=player_ids,
        style=requested_style,
    )
    if requested_style != "default":
        missing_ids = [pid for pid in player_ids if pid not in image_urls_by_id]
        fallback_urls = await get_current_image_urls_for_players(
            db,
            player_ids=missing_ids,
            style="default",
        )
        image_urls_by_id.update(fallback_urls)

    players = []
    for p in db_players:
        if p.id is None:
            continue
        img_url = image_urls_by_id.get(
            p.id,
            get_placeholder_url(
                p.display_name or "Player", player_id=p.id, width=320, height=420
            ),
        )

        players.append(
            {
                "name": p.display_name or "",
                "slug": p.slug or "",
                "position": "",  # Position data requires PlayerStatus join - to be added
                "college": p.school or "",
                "img": img_url,
                "change": 0,  # No change data without consensus rankings
                "measurables": {
                    "ht": 80,  # Placeholder - will be populated from real data later
                    "ws": 84,
                    "vert": 36,
                },
            }
        )

    # Fetch news feed from database (falls back to empty if no items yet)
    # Fetch more items to enable pagination (6 per page in new grid layout)
    news_feed = await get_news_feed(db, limit=100)
    feed_items = [
        {
            "id": item.id,
            "source": item.source_name,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "image_url": item.image_url,
            "author": item.author,
            "time": item.time,
            "tag": item.tag,
            "read_more_text": item.read_more_text,
        }
        for item in news_feed.items
    ]

    # Fetch hero article (most recent article with image)
    hero_article = await get_hero_article(db)
    hero_article_dict = None
    if hero_article:
        hero_article_dict = {
            "id": hero_article.id,
            "source": hero_article.source_name,
            "title": hero_article.title,
            "summary": hero_article.summary,
            "url": hero_article.url,
            "image_url": hero_article.image_url,
            "author": hero_article.author,
            "time": hero_article.time,
            "tag": hero_article.tag,
        }

    # Fetch source and author counts for sidebar
    source_counts = await get_source_counts(db, limit=10)
    author_counts = await get_author_counts(db, limit=10)

    # Build mappings for JS image URL generation
    slug_to_id = {slug: info[0] for slug, info in player_id_map.items()}
    id_to_slug = {info[0]: slug for slug, info in player_id_map.items()}

    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "players": players,
            "feed_items": feed_items,
            "hero_article": hero_article_dict,
            "source_counts": source_counts,
            "author_counts": author_counts,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
            "image_style": style,  # Current image style for JS
            "player_id_map": slug_to_id,  # slug -> player_id for JS image URLs
            "id_to_slug_map": id_to_slug,  # player_id -> slug for JS image URLs
            "s3_image_base_url": get_s3_image_base_url(),  # S3 base URL for images
        },
    )


@router.get("/players/{slug}", response_class=HTMLResponse)
async def player_detail(
    request: Request,
    slug: str,
    style: Optional[str] = Query(
        None, description="Image style: default, vector, comic, retro"
    ),
    db: AsyncSession = Depends(get_session),
):
    """Render the Player Detail page with bio, scoreboard, percentiles, comps, and news.

    Uses slug-based routing (e.g., /players/cooper-flagg).
    For duplicate names, append a numeric suffix (e.g., john-smith-2).
    """
    # Fetch player profile from database
    player_profile = await get_player_profile_by_slug(db, slug)

    if not player_profile:
        raise HTTPException(status_code=404, detail="Player not found")

    # Helper to filter out literal "null"/empty strings from raw data
    def clean_null(value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or text.lower() in {"null", "none"}:
            return None
        return text

    # Build player dict for template
    player_name = player_profile.display_name or "Unknown Player"

    requested_style = style or "default"
    requested_photo_url = await get_current_image_url_for_player(
        db,
        player_id=player_profile.id,
        style=requested_style,
    )
    if requested_photo_url is None and requested_style != "default":
        requested_photo_url = await get_current_image_url_for_player(
            db,
            player_id=player_profile.id,
            style="default",
        )
    if requested_photo_url is None:
        requested_photo_url = get_placeholder_url(
            player_profile.display_name,
            player_id=player_profile.id,
        )

    player = {
        "id": player_profile.id,
        "slug": player_profile.slug,
        "name": player_name,
        "position": player_profile.position,
        "college": clean_null(player_profile.school),
        "high_school": clean_null(player_profile.high_school),
        "shoots": clean_null(player_profile.shoots),
        "height": player_profile.height_formatted,
        "weight": player_profile.weight_formatted,
        "age": player_profile.age_formatted,
        "hometown": player_profile.hometown,
        "wingspan": player_profile.wingspan_formatted,
        "photo_url": requested_photo_url,
        # Metrics set to None to hide scoreboard (no data sources yet)
        "metrics": {
            "consensusRank": None,
            "consensusChange": None,
            "buzzScore": None,
            "truePosition": None,
            "trueRange": None,
            "winsAdded": None,
            "trendDirection": None,
        },
    }

    percentile_data = {
        "anthropometrics": [
            {"metric": "Height", "value": "6'9\"", "percentile": 92, "unit": ""},
            {"metric": "Weight", "value": "205", "percentile": 78, "unit": " lbs"},
            {"metric": "Wingspan", "value": "7'2\"", "percentile": 95, "unit": ""},
            {
                "metric": "Standing Reach",
                "value": "9'2\"",
                "percentile": 94,
                "unit": "",
            },
        ],
        "combinePerformance": [
            {
                "metric": "Lane Agility",
                "value": "10.84",
                "percentile": 89,
                "unit": " sec",
            },
            {"metric": "3/4 Sprint", "value": "3.15", "percentile": 91, "unit": " sec"},
            {
                "metric": "Max Vertical",
                "value": "36.0",
                "percentile": 87,
                "unit": " in",
            },
            {
                "metric": "Standing Vertical",
                "value": "32.5",
                "percentile": 85,
                "unit": " in",
            },
        ],
        "advancedStats": [
            {
                "metric": "Points Per Game",
                "value": "21.4",
                "percentile": 96,
                "unit": " PPG",
            },
            {
                "metric": "Rebounds Per Game",
                "value": "8.9",
                "percentile": 93,
                "unit": " RPG",
            },
            {
                "metric": "Assists Per Game",
                "value": "4.2",
                "percentile": 88,
                "unit": " APG",
            },
            {"metric": "PER", "value": "28.6", "percentile": 97, "unit": ""},
        ],
    }

    # Comparison data is fetched via API (GET /api/players/{slug}/similar)
    comparison_data: list = []

    # Fetch news feed (player-specific filtering would require player_id once implemented)
    # For now, show general feed on player pages too
    # Fetch more items to enable pagination (10 per page)
    news_feed = await get_news_feed(db, limit=100)
    player_feed = [
        {
            "id": item.id,
            "source": item.source_name,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "image_url": item.image_url,
            "author": item.author,
            "time": item.time,
            "tag": item.tag,
            "read_more_text": item.read_more_text,
        }
        for item in news_feed.items
    ]

    return request.app.state.templates.TemplateResponse(
        "player-detail.html",
        {
            "request": request,
            "player": player,
            "percentile_data": percentile_data,
            "comparison_data": comparison_data,
            "player_feed": player_feed,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
            "image_style": style,  # Current image style for JS
            "s3_image_base_url": get_s3_image_base_url(),  # S3 base URL for images
        },
    )


# Legal pages
@router.get("/terms", response_class=HTMLResponse)
async def terms_of_service(request: Request):
    """Render the Terms of Service page."""
    return request.app.state.templates.TemplateResponse(
        "legal/terms.html",
        {
            "request": request,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
            "current_date": datetime.now().strftime("%B %d, %Y"),
        },
    )


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy(request: Request):
    """Render the Privacy Policy page."""
    return request.app.state.templates.TemplateResponse(
        "legal/privacy.html",
        {
            "request": request,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
            "current_date": datetime.now().strftime("%B %d, %Y"),
        },
    )


@router.get("/cookies", response_class=HTMLResponse)
async def cookie_policy(request: Request):
    """Render the Cookie Policy page."""
    return request.app.state.templates.TemplateResponse(
        "legal/cookies.html",
        {
            "request": request,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
            "current_date": datetime.now().strftime("%B %d, %Y"),
        },
    )

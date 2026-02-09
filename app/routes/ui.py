"""UI Routes - Renders Jinja templates for the frontend."""

from collections import Counter
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.news_service import (
    get_hero_article,
    get_news_feed,
    get_player_news_feed,
    get_trending_players,
)
from app.config import settings
from app.services.player_service import get_player_profile_by_slug
from app.utils.db_async import get_session
from app.utils.images import (
    get_placeholder_url,
    get_player_image_url,
    get_s3_image_base_url,
)

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

# Homepage news/feed constants
HOME_NEWS_FEED_LIMIT = 100
HOME_NEWS_SIDEBAR_LIMIT = 8


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    style: Optional[str] = Query(
        None,
        description="Preferred image style (falls back to default, then placeholder)",
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

    requested_style = style or settings.default_image_style

    players = []
    for p in db_players:
        if p.id is None or not p.slug:
            continue
        img_url = get_player_image_url(
            player_id=p.id,
            slug=p.slug,
            style=requested_style,
        )
        img_default_url = get_player_image_url(
            player_id=p.id,
            slug=p.slug,
            style="default",
        )
        img_placeholder_url = get_placeholder_url(
            p.display_name or "Player",
            player_id=p.id,
            width=320,
            height=420,
        )

        players.append(
            {
                "id": p.id,
                "name": p.display_name or "",
                "slug": p.slug or "",
                "position": "",  # Position data requires PlayerStatus join - to be added
                "college": p.school or "",
                "img": img_url,
                "img_default": img_default_url,
                "img_placeholder": img_placeholder_url,
                "change": 0,  # No change data without consensus rankings
                "measurables": {
                    "ht": 80,  # Placeholder - will be populated from real data later
                    "ws": 84,
                    "vert": 36,
                },
            }
        )

    # Fetch trending players based on recent mentions
    trending_raw = await get_trending_players(db, days=7, limit=10)
    trending_players = [
        {
            "player_id": tp.player_id,
            "display_name": tp.display_name,
            "slug": tp.slug,
            "school": tp.school or "",
            "mention_count": tp.mention_count,
            "trending_score": tp.trending_score,
            "daily_counts": tp.daily_counts,
        }
        for tp in trending_raw
    ]

    # Fetch news feed from database (falls back to empty if no items yet)
    # Fetch more items to enable pagination (6 per page in new grid layout)
    news_feed = await get_news_feed(db, limit=HOME_NEWS_FEED_LIMIT)
    source_counter: Counter[str] = Counter()
    author_counter: Counter[str] = Counter()
    feed_items: list[dict] = []
    for item in news_feed.items:
        source = item.source_name.strip()
        author = (item.author or "").strip() or None

        source_counter[source] += 1
        if author:
            author_counter[author] += 1

        feed_items.append(
            {
                "id": item.id,
                "source": source,
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "image_url": item.image_url,
                "author": author,
                "time": item.time,
                "tag": item.tag,
                "read_more_text": item.read_more_text,
            }
        )

    # Fetch hero article (most recent article with image)
    hero_article = await get_hero_article(db)
    hero_article_dict = None
    if hero_article:
        hero_author = (hero_article.author or "").strip() or None
        hero_article_dict = {
            "id": hero_article.id,
            "source": hero_article.source_name.strip(),
            "title": hero_article.title,
            "summary": hero_article.summary,
            "url": hero_article.url,
            "image_url": hero_article.image_url,
            "author": hero_author,
            "time": hero_article.time,
            "tag": hero_article.tag,
        }

    # Source/author counts should align with the latest-feed window rendered on the page.
    source_counts = [
        {"source_name": source_name, "count": count}
        for source_name, count in sorted(
            source_counter.items(),
            key=lambda item: (-item[1], item[0].casefold()),
        )
    ]
    author_counts = [
        {"author": author, "count": count}
        for author, count in sorted(
            author_counter.items(),
            key=lambda item: (-item[1], item[0].casefold()),
        )
    ]

    # Build mappings for JS image URL generation
    slug_to_id = {slug: player_id for slug, (player_id, _) in player_id_map.items()}
    id_to_slug = {player_id: slug for slug, (player_id, _) in player_id_map.items()}

    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "players": players,
            "trending_players": trending_players,
            "feed_items": feed_items,
            "hero_article": hero_article_dict,
            "source_counts": source_counts,
            "author_counts": author_counts,
            "sidebar_limit": HOME_NEWS_SIDEBAR_LIMIT,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
            "image_style": requested_style,  # Current image style for JS
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
        None,
        description="Preferred image style (falls back to default, then placeholder)",
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

    requested_style = style or settings.default_image_style
    requested_photo_url = (
        get_player_image_url(
            player_id=player_profile.id,
            slug=player_profile.slug,
            style=requested_style,
        )
        if player_profile.id is not None and player_profile.slug
        else ""
    )
    fallback_photo_url = (
        get_player_image_url(
            player_id=player_profile.id,
            slug=player_profile.slug,
            style="default",
        )
        if player_profile.id is not None and player_profile.slug
        else ""
    )
    placeholder_photo_url = get_placeholder_url(
        player_name,
        player_id=player_profile.id,
        width=400,
        height=533,
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
        "photo_url_default": fallback_photo_url,
        "photo_url_placeholder": placeholder_photo_url,
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

    # Fetch player-specific news feed (mentions + direct player_id association)
    # Falls back to general feed when insufficient player-specific articles
    news_feed = await get_player_news_feed(
        db,
        player_id=player_profile.id,  # type: ignore[arg-type]
        limit=100,
        min_items=10,
    )
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
            "is_player_specific": item.is_player_specific,
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
            "image_style": requested_style,  # Current image style for JS
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

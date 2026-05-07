"""UI Routes - Renders Jinja templates for the frontend."""

from collections import Counter
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.expanded_trending_service import get_expanded_trending_players
from app.services.news_service import (
    format_relative_time,
    get_author_counts,
    get_filtered_news_feed,
    get_hero_article,
    get_news_feed,
    get_player_news_feed,
    get_source_counts,
    get_trending_players,
)
from app.schemas.player_content_mentions import ContentType
from app.services.podcast_service import (
    get_latest_podcast_episodes,
    get_player_podcast_feed,
    get_podcast_page_data,
)
from app.services.video_service import (
    get_global_video_counts_by_tag,
    get_latest_videos_by_tag,
    get_player_video_counts_by_tag,
    get_player_video_feed,
    get_video_page_data,
)
from sqlmodel import select

from app.config import settings
from app.models.fields import MetricSource
from app.schemas.metrics import MetricSnapshot
from app.schemas.seasons import Season
from app.services.combine_score_service import (
    get_player_combine_scores,
    grade_label,
)
from app.services.player_service import (
    get_college_stats_by_player_id,
    get_player_profile_by_slug,
)
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


# Homepage news/feed constants
HOME_NEWS_FEED_LIMIT = 100
HOME_NEWS_SIDEBAR_LIMIT = 8
HOME_FILM_ROOM_LIMIT = 24


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Render the Homepage with expanded trending players, VS arena, and news feed."""
    # Fetch expanded trending payload (featured cards + compact tail).
    expanded = await get_expanded_trending_players(db)
    featured_trending = [
        {
            "player_id": fp.player_id,
            "rank": fp.rank,
            "display_name": fp.display_name,
            "slug": fp.slug,
            "photo_url": fp.photo_url,
            "school": fp.school,
            "position": fp.position,
            "draft_year": fp.draft_year,
            "mention_count": fp.mention_count,
            "daily_counts": fp.daily_counts,
            "spike_state": fp.spike_state,
            "content_mix": fp.content_mix,
            "dominant_news_tag": fp.dominant_news_tag,
            "combine_grade": fp.combine_grade,
            "latest_stats": {
                "season": fp.latest_stats.season,
                "ppg": fp.latest_stats.ppg,
                "rpg": fp.latest_stats.rpg,
                "apg": fp.latest_stats.apg,
                "spg": fp.latest_stats.spg,
                "bpg": fp.latest_stats.bpg,
                "fg_pct": fp.latest_stats.fg_pct,
                "three_p_pct": fp.latest_stats.three_p_pct,
                "ft_pct": fp.latest_stats.ft_pct,
            },
            "recent_mentions": [
                {
                    "title": m.title,
                    "url": m.url,
                    "source_name": m.source_name,
                    "content_type": m.content_type,
                    "time": format_relative_time(m.published_at),
                }
                for m in fp.recent_mentions
            ],
            "latest_mention_time": (
                format_relative_time(fp.latest_mention_at)
                if fp.latest_mention_at is not None
                else None
            ),
        }
        for fp in expanded.featured
    ]
    compact_trending = [
        {
            "player_id": cp.player_id,
            "rank": cp.rank,
            "display_name": cp.display_name,
            "slug": cp.slug,
            "photo_url": cp.photo_url,
            "school": cp.school,
            "position": cp.position,
            "draft_year": cp.draft_year,
            "mention_count": cp.mention_count,
            "daily_counts": cp.daily_counts,
            "dominant_news_tag": cp.dominant_news_tag,
        }
        for cp in expanded.compact
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

    # Fetch latest podcast episodes for homepage section
    podcast_episodes_raw = await get_latest_podcast_episodes(db, limit=6)
    podcast_episodes = [
        {
            "id": ep.id,
            "show_name": ep.show_name,
            "artwork_url": ep.artwork_url,
            "show_artwork_url": ep.show_artwork_url,
            "title": ep.title,
            "summary": ep.summary,
            "tag": ep.tag,
            "audio_url": ep.audio_url,
            "episode_url": ep.episode_url,
            "duration": ep.duration,
            "time": ep.time,
            "listen_on_text": ep.listen_on_text,
            "mentioned_players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "slug": p.slug,
                }
                for p in ep.mentioned_players
            ],
        }
        for ep in podcast_episodes_raw
    ]

    # Fetch latest videos for homepage film-room section
    film_room_raw = await get_latest_videos_by_tag(db, limit=HOME_FILM_ROOM_LIMIT)
    film_room_video_counts = await get_global_video_counts_by_tag(db)
    film_room_videos = [
        {
            "id": item.id,
            "channel_name": item.channel_name,
            "thumbnail_url": item.thumbnail_url,
            "title": item.title,
            "summary": item.summary,
            "tag": item.tag,
            "youtube_url": item.youtube_url,
            "youtube_embed_id": item.youtube_embed_id,
            "duration": item.duration,
            "time": item.time,
            "view_count_display": item.view_count_display,
            "watch_on_text": item.watch_on_text,
            "mentioned_players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "slug": p.slug,
                }
                for p in item.mentioned_players
            ],
        }
        for item in film_room_raw
    ]

    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "featured_trending": featured_trending,
            "compact_trending": compact_trending,
            "feed_items": feed_items,
            "hero_article": hero_article_dict,
            "source_counts": source_counts,
            "author_counts": author_counts,
            "sidebar_limit": HOME_NEWS_SIDEBAR_LIMIT,
            "podcast_episodes": podcast_episodes,
            "film_room_videos": film_room_videos,
            "film_room_video_counts": film_room_video_counts,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


PODCAST_PAGE_LIMIT = 10
FILM_ROOM_PAGE_LIMIT = 12


@router.get("/podcasts", response_class=HTMLResponse)
async def podcasts_page(
    request: Request,
    offset: int = Query(0, ge=0),
    tag: str | None = Query(default=None),
    show: int | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
):
    """Render the dedicated Podcasts page with feed, sidebar, and filtering."""
    page_data = await get_podcast_page_data(
        db, limit=PODCAST_PAGE_LIMIT, offset=offset, tag=tag, show_id=show
    )

    feed = page_data["feed"]
    shows = page_data["shows"]
    trending_raw = page_data["trending"]

    episodes = [
        {
            "id": ep.id,
            "show_name": ep.show_name,
            "artwork_url": ep.artwork_url,
            "show_artwork_url": ep.show_artwork_url,
            "title": ep.title,
            "summary": ep.summary,
            "tag": ep.tag,
            "audio_url": ep.audio_url,
            "episode_url": ep.episode_url,
            "duration": ep.duration,
            "time": ep.time,
            "listen_on_text": ep.listen_on_text,
            "mentioned_players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "slug": p.slug,
                }
                for p in ep.mentioned_players
            ],
        }
        for ep in feed.items
    ]

    shows_data = [
        {
            "id": s.id,
            "name": s.display_name,
            "artwork_url": s.artwork_url,
        }
        for s in shows
    ]

    trending_players = [
        {
            "player_id": tp.player_id,
            "display_name": tp.display_name,
            "slug": tp.slug,
            "mention_count": tp.mention_count,
        }
        for tp in trending_raw
    ]

    return request.app.state.templates.TemplateResponse(
        "podcasts.html",
        {
            "request": request,
            "episodes": episodes,
            "shows": shows_data,
            "trending_players": trending_players,
            "total": feed.total,
            "limit": PODCAST_PAGE_LIMIT,
            "offset": offset,
            "active_tag": tag,
            "active_show": show,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/film-room", response_class=HTMLResponse)
async def film_room_page(
    request: Request,
    offset: int = Query(0, ge=0),
    tag: str | None = Query(default=None),
    channel: int | None = Query(default=None),
    player: int | None = Query(default=None),
    search: str | None = Query(default=None),
    response_format: str | None = Query(default=None, alias="format"),
    db: AsyncSession = Depends(get_session),
):
    """Render the dedicated Film Room page."""
    page_data = await get_video_page_data(
        db=db,
        limit=FILM_ROOM_PAGE_LIMIT,
        offset=offset,
        tag=tag,
        channel_id=channel,
        player_id=player,
        search=search,
    )
    feed = page_data["feed"]
    channels = page_data["channels"]
    trending_raw = page_data["trending"]
    stats = page_data["stats"]

    videos = [
        {
            "id": item.id,
            "channel_name": item.channel_name,
            "channel_url": item.channel_url,
            "thumbnail_url": item.thumbnail_url,
            "title": item.title,
            "summary": item.summary,
            "tag": item.tag,
            "youtube_url": item.youtube_url,
            "youtube_embed_id": item.youtube_embed_id,
            "duration": item.duration,
            "time": item.time,
            "view_count_display": item.view_count_display,
            "watch_on_text": item.watch_on_text,
            "is_player_specific": item.is_player_specific,
            "mentioned_players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "slug": p.slug,
                }
                for p in item.mentioned_players
            ],
        }
        for item in feed.items
    ]
    channels_data = [
        {
            "id": c.id,
            "name": c.display_name,
            "channel_url": c.channel_url,
            "thumbnail_url": c.thumbnail_url,
        }
        for c in channels
    ]
    trending_players = [
        {
            "player_id": tp.player_id,
            "display_name": tp.display_name,
            "slug": tp.slug,
            "mention_count": tp.mention_count,
        }
        for tp in trending_raw
    ]

    if response_format == "json":
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "videos": videos,
                "total": feed.total,
                "offset": offset,
                "limit": FILM_ROOM_PAGE_LIMIT,
                "has_more": offset + FILM_ROOM_PAGE_LIMIT < feed.total,
            }
        )

    return request.app.state.templates.TemplateResponse(
        "film-room.html",
        {
            "request": request,
            "videos": videos,
            "channels": channels_data,
            "trending_players": trending_players,
            "total": feed.total,
            "channel_total": stats["channel_total"],
            "trending_total": stats["trending_total"],
            "limit": FILM_ROOM_PAGE_LIMIT,
            "offset": offset,
            "active_tag": tag,
            "active_channel": channel,
            "active_player": player,
            "search_query": search or "",
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


NEWS_PAGE_LIMIT = 12


@router.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    offset: int = Query(0, ge=0),
    tag: str | None = Query(default=None),
    source: int | None = Query(default=None),
    author: str | None = Query(default=None),
    player: int | None = Query(default=None),
    period: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
):
    """Render the dedicated News page with filterable article feed."""
    # Fetch filtered news feed
    feed = await get_filtered_news_feed(
        db,
        limit=NEWS_PAGE_LIMIT,
        offset=offset,
        tag=tag,
        source_id=source,
        author=author,
        player_id=player,
        period=period,
    )

    feed_items: list[dict] = []
    for item in feed.items:
        item_source = item.source_name.strip()
        item_author = (item.author or "").strip() or None
        feed_items.append(
            {
                "id": item.id,
                "source": item_source,
                "title": item.title,
                "summary": item.summary,
                "url": item.url,
                "image_url": item.image_url,
                "author": item_author,
                "time": item.time,
                "tag": item.tag,
                "read_more_text": item.read_more_text,
            }
        )

    # Hero article: first page of any filter view
    hero_article_dict = None
    if offset == 0:
        hero_article = await get_hero_article(db)
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

    # Sidebar data
    source_counts_raw = await get_source_counts(db)
    sources_data = [
        {"id": sid, "name": name, "count": count}
        for sid, name, count in source_counts_raw
    ]

    author_counts_raw = await get_author_counts(db)
    authors_list = [{"name": name, "count": count} for name, count in author_counts_raw]

    trending_raw = await get_trending_players(
        db, days=30, limit=10, content_type=ContentType.NEWS
    )
    trending_players = [
        {
            "player_id": tp.player_id,
            "display_name": tp.display_name,
            "slug": tp.slug,
            "mention_count": tp.mention_count,
        }
        for tp in trending_raw
    ]

    # Resolve active filter labels for display
    active_source_name = None
    if source:
        for s in sources_data:
            if s["id"] == source:
                active_source_name = s["name"]
                break

    active_player_name = None
    if player:
        from sqlalchemy import select as sa_select

        from app.schemas.players_master import PlayerMaster

        result = await db.execute(
            sa_select(PlayerMaster.display_name).where(  # type: ignore[call-overload]
                PlayerMaster.id == player  # type: ignore[arg-type]
            )
        )
        active_player_name = result.scalar()

    return request.app.state.templates.TemplateResponse(
        "news.html",
        {
            "request": request,
            "feed_items": feed_items,
            "hero_article": hero_article_dict,
            "sources": sources_data,
            "authors": authors_list,
            "trending_players": trending_players,
            "total": feed.total,
            "limit": NEWS_PAGE_LIMIT,
            "offset": offset,
            "active_tag": tag,
            "active_source": source,
            "active_source_name": active_source_name,
            "active_author": author,
            "active_player": player,
            "active_player_name": active_player_name,
            "active_period": period,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
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
        "combine_year": player_profile.combine_year,
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

    # Fetch combine scores for the headline box
    combine_scores = None
    combine_grade = None
    combine_population = None
    if player.get("combine_year"):
        season_result = await db.execute(
            select(Season).where(  # type: ignore[call-overload]
                Season.start_year == player["combine_year"]
            )
        )
        season = season_result.scalars().first()
        if season:
            combine_scores = await get_player_combine_scores(
                db,
                player_profile.id,  # type: ignore[arg-type]
                season_id=season.id,
            )
            if combine_scores and combine_scores.overall_score:
                combine_grade = grade_label(combine_scores.overall_score.percentile)
                # Fetch population size from the snapshot
                snap_result = await db.execute(
                    select(MetricSnapshot.population_size)
                    .where(  # type: ignore[call-overload]
                        MetricSnapshot.source == MetricSource.combine_score,  # type: ignore[arg-type]
                        MetricSnapshot.is_current.is_(True),  # type: ignore[union-attr,attr-defined]
                        MetricSnapshot.season_id == season.id,  # type: ignore[arg-type]
                        MetricSnapshot.position_scope_parent.is_(None),  # type: ignore[union-attr]
                        MetricSnapshot.position_scope_fine.is_(None),  # type: ignore[union-attr]
                    )
                    .limit(1)
                )
                combine_population = snap_result.scalar_one_or_none()

    # Fetch college production stats for the stats scoreboard
    college_stats_rows = await get_college_stats_by_player_id(
        db,
        player_id=player_profile.id,  # type: ignore[arg-type]
    )
    # Only attach school when there's a single season — for multi-season
    # players the current school may not match earlier seasons (transfers).
    single_season = len(college_stats_rows) == 1
    college_stats = [
        {
            "season": row.season,
            "school": player.get("college") if single_season else None,
            "games": row.games,
            "games_started": row.games_started,
            "mpg": row.mpg,
            "ppg": row.ppg,
            "rpg": row.rpg,
            "apg": row.apg,
            "spg": row.spg,
            "bpg": row.bpg,
            "tov": row.tov,
            "fg_pct": row.fg_pct,
            "three_p_pct": row.three_p_pct,
            "three_pa": row.three_pa,
            "ft_pct": row.ft_pct,
            "fta": row.fta,
        }
        for row in college_stats_rows
    ]

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

    # Fetch player-specific podcast feed (mentions + direct player_id)
    podcast_feed_resp = await get_player_podcast_feed(
        db,
        player_id=player_profile.id,  # type: ignore[arg-type]
        limit=50,
    )
    player_podcast_feed = [
        {
            "id": ep.id,
            "show_name": ep.show_name,
            "artwork_url": ep.artwork_url,
            "show_artwork_url": ep.show_artwork_url,
            "title": ep.title,
            "summary": ep.summary,
            "tag": ep.tag,
            "audio_url": ep.audio_url,
            "episode_url": ep.episode_url,
            "duration": ep.duration,
            "time": ep.time,
            "listen_on_text": ep.listen_on_text,
            "is_player_specific": ep.is_player_specific,
            "mentioned_players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "slug": p.slug,
                }
                for p in ep.mentioned_players
            ],
        }
        for ep in podcast_feed_resp.items
    ]

    player_video_feed_resp = await get_player_video_feed(
        db,
        player_id=player_profile.id,  # type: ignore[arg-type]
        limit=50,
    )
    player_video_feed = [
        {
            "id": item.id,
            "channel_name": item.channel_name,
            "thumbnail_url": item.thumbnail_url,
            "title": item.title,
            "summary": item.summary,
            "tag": item.tag,
            "youtube_url": item.youtube_url,
            "youtube_embed_id": item.youtube_embed_id,
            "duration": item.duration,
            "time": item.time,
            "view_count_display": item.view_count_display,
            "watch_on_text": item.watch_on_text,
            "is_player_specific": item.is_player_specific,
            "mentioned_players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "slug": p.slug,
                }
                for p in item.mentioned_players
            ],
        }
        for item in player_video_feed_resp.items
    ]
    player_video_counts = await get_player_video_counts_by_tag(
        db,
        player_id=player_profile.id,  # type: ignore[arg-type]
    )

    return request.app.state.templates.TemplateResponse(
        "player-detail.html",
        {
            "request": request,
            "player": player,
            "college_stats": college_stats,
            "percentile_data": percentile_data,
            "comparison_data": comparison_data,
            "player_feed": player_feed,
            "player_podcast_feed": player_podcast_feed,
            "player_video_feed": player_video_feed,
            "player_video_counts": player_video_counts,
            "has_player_videos": bool(player_video_feed_resp.total),
            "combine_scores": combine_scores,
            "combine_grade": combine_grade,
            "combine_population": combine_population,
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

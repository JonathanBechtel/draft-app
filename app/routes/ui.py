"""UI Routes - Renders Jinja templates for the frontend."""

from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.expanded_trending_service import get_expanded_trending_players
from app.models.news import NewsItemRead
from app.services.news_service import (
    format_relative_time,
    get_author_counts,
    get_filtered_news_feed,
    get_hero_article,
    get_news_feed,
    get_player_news_feed,
    get_source_counts,
    get_sticky_news_item,
    get_trending_players,
)
from app.models.consensus import ConsensusRow
from app.schemas.boards import BoardKind
from app.schemas.player_content_mentions import ContentType
from app.services.consensus_read_service import (
    get_biggest_movers,
    get_board_freshness,
    get_consensus_board,
    get_mock_consensus_board,
    get_most_controversial,
    get_player_consensus_detail,
    get_rank_trajectories,
    get_source_breakdown_matrix,
    get_source_detail,
    get_source_leaderboard,
    get_source_overlays,
    get_source_spotlight,
)
from app.services.draft_results_service import (
    depth_buckets,
    get_draft_recap,
    get_recap_years,
    get_source_accuracy,
    has_draft_results,
    split_movers,
)
from app.utils.recap_charts import build_recap_scatter_svg
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

from app.config import get_consensus_board_kind, settings
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
from app.services.event_desk.timeutils import to_eastern_date
from app.services.school_logo_service import get_logo_url_for_school
from app.services.summer_league.desk_read import get_desk_view
from app.services.summer_league_metrics_service import get_player_metric_seasons
from app.services.summer_league_stats_service import (
    get_player_shotchart_context,
    get_summer_league_profile_by_player_id,
    summer_league_to_context,
)
from app.utils.db_async import get_session
from app.utils.sparkline import build_sparkline_path, sparkline_direction
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


def _news_item_to_dict(item: NewsItemRead, *, is_sticky: bool = False) -> dict:
    """Render a NewsItemRead as the dict shape feed templates expect."""
    return {
        "id": item.id,
        "source": item.source_name.strip(),
        "title": item.title,
        "summary": item.summary,
        "url": item.url,
        "image_url": item.image_url,
        "author": (item.author or "").strip() or None,
        "time": item.time,
        "tag": item.tag,
        "read_more_text": item.read_more_text,
        "is_sticky": is_sticky,
    }


# Draft year for the current cycle.  Update each off-season.
CONSENSUS_DRAFT_YEAR = 2026

# Number of consensus rows shown before the "show all" expander on the homepage.
# 14 = the lottery picks; the rest collapse to save vertical real estate.
CONSENSUS_LOTTERY_PICKS = 14


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Render the Homepage with consensus hero, trending players, VS arena, and news feed."""
    # --- Summer League Desk (event-instance #1 of the Event Desk framework) ---
    # ONE service call assembles the current-state payload (from the precomputed
    # T2/T4/event_desk_state projections) AND its player/team view-context
    # enrichment -- see `get_desk_view`'s docstring for why those two things are
    # composed there rather than in this route. `daily_state` itself is resolved
    # at request time by the framework's pure resolvers (see
    # app.services.summer_league.desk_read module docstring). A `None` payload
    # means the SL event's lifecycle isn't currently active -- the template
    # collapses to the archive-strip treatment (behavior spec §2) instead of the
    # takeover; the enrichment dicts degrade to empty in that case too.
    # `now` is naive UTC (tzinfo stripped): the framework resolvers compare it
    # against `summer_league_games.tip_datetime`, which is naive UTC by repo
    # convention, so an aware value here would raise on the comparison. Building
    # it timezone-aware first (not deprecated `utcnow()`) then dropping tzinfo
    # yields the correct wall-clock instant without an aware/naive mismatch.
    now_naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    desk_view = await get_desk_view(db, now=now_naive_utc)
    desk_payload = desk_view.payload
    desk_window_open = desk_payload is not None
    desk_game_year = to_eastern_date(now_naive_utc).year

    # --- Consensus hero -------------------------------------------------------
    # Select the board kind from the draft calendar.  Fall back to BIG_BOARD
    # when the calendar-selected kind returns no rows (e.g. MOCK_DRAFT data has
    # not yet been ingested; see ticket notes on that ticket).  This ensures
    # the hero is never empty even before mock-draft extraction ships.
    board_kind = get_consensus_board_kind()
    consensus_rows_raw = await get_consensus_board(db, draft_year=CONSENSUS_DRAFT_YEAR)
    # `get_consensus_board` returns BIG_BOARD consensus only today (mock-draft
    # consensus is produced by a later ticket). Any rows we have are therefore
    # big-board rows, so force the heading to match the data — otherwise a
    # post-lottery calendar phase would label big-board rows as "Mock Draft".
    # Once a kind-aware mock read path exists, fetch by `board_kind` here.
    if consensus_rows_raw:
        board_kind = BoardKind.BIG_BOARD

    # Derive snapshot_computed_at from the first row's data is not directly
    # available on ConsensusRow; pass None (template shows nothing when absent).
    snapshot_computed_at: datetime | None = None

    consensus_rows = [
        {
            "player_id": r.player_id,
            "player_name": r.player_name,
            "school": r.school,
            "slug": r.slug,
            "photo_url": r.photo_url,
            "school_logo_url": r.school_logo_url,
            "age": r.age,
            "position": r.position,
            "height": r.height,
            "weight": r.weight,
            "consensus_rank": r.consensus_rank,
            "avg_rank": r.avg_rank,
            "high_rank": r.high_rank,
            "low_rank": r.low_rank,
            "num_sources": r.num_sources,
            # rank_delta: None when no prior snapshot (single-snapshot case).
            # Positive delta = moved up (lower rank number), negative = moved down.
            "rank_delta": r.rank_delta,
            "prev_rank": r.prev_rank,
            # Sparkline trajectory across recent snapshots; oldest-first.
            "recent_ranks": r.recent_ranks,
            "sparkline_path": build_sparkline_path(r.recent_ranks),
            "sparkline_direction": sparkline_direction(r.recent_ranks),
        }
        for r in consensus_rows_raw
    ]

    # --- Supporting panels: Biggest Movers, Most Controversial, Source Spotlight
    # Movers are trimmed to 3-up per direction so the panel reads at a glance;
    # Board Freshness is rendered as a footnote rather than its own card.
    biggest_movers = await get_biggest_movers(db, draft_year=CONSENSUS_DRAFT_YEAR, k=3)
    most_controversial = await get_most_controversial(
        db, draft_year=CONSENSUS_DRAFT_YEAR, limit=5
    )
    source_spotlight = await get_source_spotlight(db, draft_year=CONSENSUS_DRAFT_YEAR)
    board_freshness = await get_board_freshness(db, draft_year=CONSENSUS_DRAFT_YEAR)
    # Analysts whose boards feed the consensus — credited in the attribution note.
    attribution_sources = await get_source_leaderboard(
        db, draft_year=CONSENSUS_DRAFT_YEAR
    )

    # --- Expanded trending payload (featured cards + compact tail) ------------
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
    sticky_item = await get_sticky_news_item(db)
    source_counter: Counter[str] = Counter()
    author_counter: Counter[str] = Counter()
    feed_items: list[dict] = []
    if sticky_item is not None:
        feed_items.append(_news_item_to_dict(sticky_item, is_sticky=True))
    sticky_id = sticky_item.id if sticky_item is not None else None
    for item in news_feed.items:
        if item.id == sticky_id:
            # Already prepended via the sticky lookup; skip the duplicate.
            continue
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
                "is_sticky": False,
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
            # Summer League Desk (None/False off-window -> collapsed strip)
            "desk_payload": desk_payload,
            "desk_window_open": desk_window_open,
            "desk_players": desk_view.players,
            "desk_matchups": desk_view.matchups,
            "desk_game_year": desk_game_year,
            # Consensus hero
            "board_kind": board_kind,
            "consensus_rows": consensus_rows,
            "snapshot_computed_at": snapshot_computed_at,
            "draft_year": CONSENSUS_DRAFT_YEAR,
            "consensus_lottery_picks": CONSENSUS_LOTTERY_PICKS,
            # Supporting panels
            "biggest_movers": biggest_movers,
            "most_controversial": most_controversial,
            "source_spotlight": source_spotlight,
            "board_freshness": board_freshness,
            "attribution_sources": attribution_sources,
            # Existing sections
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
            "image_style": settings.default_image_style,
            "s3_image_base_url": get_s3_image_base_url(),
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
    # Sticky pins to the top of the default /news view only: zero active
    # filters. Once the user narrows the feed, the pin steps aside.
    has_filters = any(
        v is not None and v != "" for v in (tag, source, author, player, period)
    )
    sticky_item: NewsItemRead | None = None
    if not has_filters:
        sticky_item = await get_sticky_news_item(db)
    sticky_id = sticky_item.id if sticky_item is not None else None

    # When a sticky is in play, page 1 surrenders one slot to it (limit-1
    # natural results + sticky card = NEWS_PAGE_LIMIT cards) and pages 2+
    # shift their offset back by 1 to backfill the slot that page 1 did
    # not consume. The query also drops the sticky id outright so it
    # cannot reappear at its natural position on a later page.
    sticky_consumed_a_slot = 1 if sticky_id is not None else 0
    feed_limit = NEWS_PAGE_LIMIT - (sticky_consumed_a_slot if offset == 0 else 0)
    feed_offset = offset - sticky_consumed_a_slot if offset > 0 else 0

    feed = await get_filtered_news_feed(
        db,
        limit=feed_limit,
        offset=feed_offset,
        tag=tag,
        source_id=source,
        author=author,
        player_id=player,
        period=period,
        exclude_id=sticky_id,
    )

    feed_items: list[dict] = []
    if sticky_item is not None and offset == 0:
        feed_items.append(_news_item_to_dict(sticky_item, is_sticky=True))
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
                "is_sticky": False,
            }
        )

    # Pagination math runs on the full feed including the sticky card, so
    # the page links land on the right offsets and "X-Y of Z" matches the
    # number of cards a user actually scrolls past.
    total = feed.total + sticky_consumed_a_slot

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
            "total": total,
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

    school_name = clean_null(player_profile.school)
    school_logo_url = await get_logo_url_for_school(db, school_name)

    player = {
        "id": player_profile.id,
        "slug": player_profile.slug,
        "name": player_name,
        "position": player_profile.position,
        "college": school_name,
        "school_logo_url": school_logo_url,
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
            "school_logo_url": school_logo_url if single_season else None,
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
            # Makes aren't stored for college; derive per-game 3PM/FTM from the
            # attempts and the shooting % (0-100 scale) so the volume shows
            # alongside the rate. (FG volume is unrecoverable: only fg_pct exists.)
            "three_pm": (
                row.three_pa * row.three_p_pct / 100.0
                if row.three_pa is not None and row.three_p_pct is not None
                else None
            ),
            "ft_pct": row.ft_pct,
            "fta": row.fta,
            "ftm": (
                row.fta * row.ft_pct / 100.0
                if row.fta is not None and row.ft_pct is not None
                else None
            ),
        }
        for row in college_stats_rows
    ]

    # Fetch Summer League production for the SL scoreboard section.
    # None (player not resolved to any SL game log) => omit the section.
    summer_league = None
    if player_profile.id is not None:
        sl_profile = await get_summer_league_profile_by_player_id(
            db, player_id=player_profile.id
        )
        if sl_profile is not None:
            summer_league = summer_league_to_context(sl_profile)

    # SL-calibrated advanced metrics (PER / WS / BPM / VORP / ratings) from the
    # materialized per-competition table. None when the player has no
    # adv-eligible competition => the advanced sub-table is omitted.
    sl_metrics = None
    if player_profile.id is not None:
        sl_metrics = await get_player_metric_seasons(db, player_id=player_profile.id)

    # Shot chart + shot-diet (career rollup): zones aggregated across all
    # competitions; no pool colours and no dots (career mixes pools).  None
    # when the player has no parsed shot events => chart section omitted.
    sl_shotchart: dict | None = None
    if player_profile.id is not None:
        sl_shotchart = await get_player_shotchart_context(
            db, player_id=player_profile.id, competition_id=None
        )

    # Fetch consensus detail for this player.
    # Treat a None result (player not on any board) as "omit the section" — do
    # NOT raise 404; the rest of the page must still render normally.
    consensus: dict | None = None
    if player_profile.id is not None:
        consensus_detail = await get_player_consensus_detail(
            db,
            player_id=player_profile.id,
            draft_year=CONSENSUS_DRAFT_YEAR,
        )
        if consensus_detail is not None:
            consensus = {
                "current": {
                    "consensus_rank": consensus_detail.consensus_rank,
                    "avg_rank": consensus_detail.avg_rank,
                    "high_rank": consensus_detail.high_rank,
                    "low_rank": consensus_detail.low_rank,
                    "num_sources": consensus_detail.num_sources,
                    "prev_rank": consensus_detail.prev_rank,
                    "rank_delta": consensus_detail.rank_delta,
                },
                "sources": [
                    {
                        "source_display_name": s.source_display_name,
                        "source_rank": s.source_rank,
                        "article_url": s.article_url,
                        "article_title": s.article_title,
                    }
                    for s in consensus_detail.source_ranks
                ],
                "history": [
                    {
                        "computed_at": h.computed_at.isoformat(),
                        "consensus_rank": h.consensus_rank,
                    }
                    for h in consensus_detail.rank_history
                ],
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
            "consensus": consensus,
            "college_stats": college_stats,
            "summer_league": summer_league,
            "sl_metrics": sl_metrics,
            "sl_shotchart": sl_shotchart,
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


@router.get("/consensus", response_class=HTMLResponse)
async def consensus_page(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Render the dedicated Consensus page with full board, scatter, sources, matrix, and trajectories.

    Fetches the complete context for all sections in a single route handler so
    downstream partial-section templates receive a fully-populated context dict.
    The board kind is calendar-determined; when big-board rows exist the heading
    is forced to BIG_BOARD (same post-lottery fallback as the homepage hero).
    """
    # --- Board kind (calendar-determined; forced to data kind when rows exist) -
    board_kind = get_consensus_board_kind()

    # Post-lottery mock-draft view: when the team-overlay flag is enabled and
    # the calendar is past the lottery, render the unified consensus as a mock
    # draft with each row's owning team. Otherwise keep the big-board view
    # (and the post-lottery fallback that forces the BIG_BOARD heading when
    # rows exist, since the consensus data itself is kind-agnostic).
    mock_overlay = (
        settings.mock_draft_team_overlay_enabled and board_kind == BoardKind.MOCK_DRAFT
    )

    # --- Full consensus board --------------------------------------------------
    consensus_rows_raw: Sequence[ConsensusRow]
    if mock_overlay:
        consensus_rows_raw = await get_mock_consensus_board(
            db, draft_year=CONSENSUS_DRAFT_YEAR
        )
    else:
        consensus_rows_raw = await get_consensus_board(
            db, draft_year=CONSENSUS_DRAFT_YEAR
        )
        if consensus_rows_raw:
            board_kind = BoardKind.BIG_BOARD

    consensus_rows = [
        {
            "player_id": r.player_id,
            "player_name": r.player_name,
            "school": r.school,
            "slug": r.slug,
            "photo_url": r.photo_url,
            "school_logo_url": r.school_logo_url,
            "age": r.age,
            "position": r.position,
            "height": r.height,
            "weight": r.weight,
            "consensus_rank": r.consensus_rank,
            "avg_rank": r.avg_rank,
            "high_rank": r.high_rank,
            "low_rank": r.low_rank,
            "num_sources": r.num_sources,
            "rank_delta": r.rank_delta,
            "prev_rank": r.prev_rank,
            "recent_ranks": r.recent_ranks,
            "sparkline_path": build_sparkline_path(r.recent_ranks),
            "sparkline_direction": sparkline_direction(r.recent_ranks),
            # Team overlay (mock-draft view only; None on the big-board view).
            "team_name": getattr(r, "team_name", None),
            "team_abbreviation": getattr(r, "team_abbreviation", None),
            "team_slug": getattr(r, "team_slug", None),
            "team_logo_url": getattr(r, "team_logo_url", None),
            "team_primary_color": getattr(r, "team_primary_color", None),
            "original_team_abbreviation": getattr(
                r, "original_team_abbreviation", None
            ),
            "trade_note": getattr(r, "trade_note", None),
        }
        for r in consensus_rows_raw
    ]

    # --- Supporting panels: freshness, movers, controversial, spotlight --------
    # The consensus board (``consensus_rows_raw``) is reused by the spotlight and
    # the per-source overlays below so neither has to rebuild it.
    board_freshness = await get_board_freshness(db, draft_year=CONSENSUS_DRAFT_YEAR)
    biggest_movers = await get_biggest_movers(db, draft_year=CONSENSUS_DRAFT_YEAR, k=5)
    most_controversial = await get_most_controversial(
        db, draft_year=CONSENSUS_DRAFT_YEAR, limit=5
    )
    source_spotlight = await get_source_spotlight(
        db, draft_year=CONSENSUS_DRAFT_YEAR, consensus_rows=list(consensus_rows_raw)
    )

    # --- Source leaderboard + per-source overlays -----------------------------
    source_leaderboard = await get_source_leaderboard(
        db, draft_year=CONSENSUS_DRAFT_YEAR
    )

    # Per-source overlays (agreement scatter + source-detail section), built in
    # one batched pass over the prebuilt consensus board instead of re-running
    # the full source-detail pipeline — and rebuilding the board — per source.
    source_overlays = await get_source_overlays(
        db, draft_year=CONSENSUS_DRAFT_YEAR, consensus_rows=list(consensus_rows_raw)
    )

    # --- Matrix + trajectories (ticket #270 additions) ------------------------
    # 14 rows = the full lottery (top-14 picks).
    source_matrix = await get_source_breakdown_matrix(
        db, draft_year=CONSENSUS_DRAFT_YEAR, top_n=14
    )
    rank_trajectories = await get_rank_trajectories(
        db, draft_year=CONSENSUS_DRAFT_YEAR, top_n=14
    )

    # Post-draft handoff: once actual picks land, surface a banner to the recap.
    draft_is_in = await has_draft_results(db, draft_year=CONSENSUS_DRAFT_YEAR)

    return request.app.state.templates.TemplateResponse(
        "consensus.html",
        {
            "request": request,
            # Board heading
            "board_kind": board_kind,
            "draft_year": CONSENSUS_DRAFT_YEAR,
            "draft_is_in": draft_is_in,
            # Full consensus board
            "consensus_rows": consensus_rows,
            # Board freshness footnote
            "board_freshness": board_freshness,
            # Supporting panels
            "biggest_movers": biggest_movers,
            "most_controversial": most_controversial,
            "source_spotlight": source_spotlight,
            # Source leaderboard + per-source overlays (scatter / source section)
            "source_leaderboard": source_leaderboard,
            "source_overlays": source_overlays,
            # Aggregation attribution — analysts whose boards feed the consensus.
            "attribution_sources": source_leaderboard,
            # Source breakdown matrix (ticket #270)
            "source_matrix": source_matrix,
            # Player rank trajectories (ticket #270)
            "rank_trajectories": rank_trajectories,
            # Footer / global
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/draft", response_class=HTMLResponse)
async def draft_hub_page(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Draft hub: one entry point for the live consensus board and recap archive.

    Consolidates the draft surfaces under a single nav slot that scales as more
    draft years accumulate — the live board up top, the per-year recaps below.
    """
    recap_years = await get_recap_years(db)
    draft_is_in = await has_draft_results(db, draft_year=CONSENSUS_DRAFT_YEAR)
    return request.app.state.templates.TemplateResponse(
        "draft-hub.html",
        {
            "request": request,
            "draft_year": CONSENSUS_DRAFT_YEAR,
            "recap_years": recap_years,
            "draft_is_in": draft_is_in,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


async def _render_draft_recap(request: Request, db: AsyncSession, draft_year: int):
    """Build the single-page draft recap for one draft year.

    One page covers the whole story: the face scatter of how the field played
    out, the pick-by-pick board (expected-vs-actual, coloured by where each pick
    landed in its consensus range), the slid/jumped movers, predictability by
    draft range, and how each board — including the blended consensus — matched
    the real order. Renders a "picks come in tonight" preview until results land.
    """
    picks, summary = await get_draft_recap(db, draft_year=draft_year)
    later, earlier = split_movers(picks, limit=10)
    source_accuracy = await get_source_accuracy(
        db, draft_year=draft_year, min_shared=5, include_consensus=True
    )
    # The consensus is a benchmark, not a contestant: keep the numbered
    # leaderboard to the individual analyst boards, and report the blend's score
    # against them separately (how many it out-predicted) rather than ranking it.
    analysts = [r for r in source_accuracy if not r.is_consensus]
    consensus_accuracy = next((r for r in source_accuracy if r.is_consensus), None)
    consensus_beats = (
        sum(
            1
            for r in analysts
            if (r.order_match or -1) < (consensus_accuracy.order_match or 0)
        )
        if consensus_accuracy
        else 0
    )
    # Years with results power the archive switcher (only shown when >1 exists).
    recap_years = await get_recap_years(db)

    return request.app.state.templates.TemplateResponse(
        "draft-recap.html",
        {
            "request": request,
            "draft_year": draft_year,
            "recap_years": recap_years,
            "picks": picks,
            "summary": summary,
            "later_movers": later,
            "earlier_movers": earlier,
            "scatter_svg": build_recap_scatter_svg(picks),
            "depth": depth_buckets(picks),
            "analysts": analysts,
            "top_analyst": analysts[0] if analysts else None,
            "consensus_accuracy": consensus_accuracy,
            "consensus_beats": consensus_beats,
            "num_analysts": len(analysts),
            "has_results": bool(picks),
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/draft-recap", response_class=HTMLResponse)
async def draft_recap_page(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Latest draft recap — the most recent year with results, else this cycle.

    Stays a stable, shareable URL as the archive grows; individual years live at
    ``/draft-recap/<year>``.
    """
    years = await get_recap_years(db)
    draft_year = years[0] if years else CONSENSUS_DRAFT_YEAR
    return await _render_draft_recap(request, db, draft_year)


@router.get("/draft-recap/analysis", response_class=RedirectResponse)
async def draft_recap_analysis_redirect() -> RedirectResponse:
    """Permanent redirect: the analysis merged into the single recap page."""
    return RedirectResponse("/draft-recap", status_code=301)


@router.get("/draft-recap/{year}", response_class=HTMLResponse)
async def draft_recap_year_page(
    year: int,
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """A specific draft year's recap (the per-year archive entry)."""
    return await _render_draft_recap(request, db, year)


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


@router.get("/sources", response_class=HTMLResponse)
async def sources_leaderboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Render the Sources leaderboard page.

    Shows all sources ranked by contrarian score for the current snapshot,
    with avg deviation and biggest-outlier pick called out. Each row links
    to the source detail page.
    """
    board_kind = get_consensus_board_kind()
    leaderboard = await get_source_leaderboard(db, draft_year=CONSENSUS_DRAFT_YEAR)
    # Force big-board kind when data exists (same as homepage pattern —
    # mock-draft source analytics are a later ticket).
    if leaderboard:
        board_kind = BoardKind.BIG_BOARD

    return request.app.state.templates.TemplateResponse(
        "sources/index.html",
        {
            "request": request,
            "leaderboard": leaderboard,
            "board_kind": board_kind,
            "draft_year": CONSENSUS_DRAFT_YEAR,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/sources/{slug}", response_class=HTMLResponse)
async def source_detail(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_session),
):
    """Render the Source Detail page with its board vs consensus overlay.

    Shows the source's most-recent board side-by-side with the consensus,
    highlighting their biggest-outlier picks.  Returns 404 when the slug
    does not match any known source.
    """
    board_kind = get_consensus_board_kind()
    detail = await get_source_detail(
        db, source_slug=slug, draft_year=CONSENSUS_DRAFT_YEAR
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if detail.get("overlay_rows"):
        board_kind = BoardKind.BIG_BOARD

    return request.app.state.templates.TemplateResponse(
        "sources/detail.html",
        {
            "request": request,
            "source": detail,
            "board_kind": board_kind,
            "draft_year": CONSENSUS_DRAFT_YEAR,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )

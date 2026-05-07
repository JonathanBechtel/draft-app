"""Expanded trending-players service for the homepage.

Builds on the base ``get_trending_players`` query and enriches each player
with the supplementary signals required by the v2 trending card design:

* generated S3 photo (eligibility gate + display)
* canonical position + school
* latest college production line (PPG required for featured eligibility)
* combine grade letter (optional pill)
* per-content-type mention mix (NEWS / PODCAST / VIDEO)
* dominant ``NewsItemTag`` over the trailing window
* up to N most-recent mention previews (title + source + relative time)
* spike state (``hot`` / ``cooling``) derived from the daily-counts series

The result is split into a featured tier (rich 2x2 cards) and a compact
tier (smaller leaderboard rows) according to per-player eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from sqlalchemy import desc, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricSource
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.player_content_mentions import ContentType, PlayerContentMention
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.podcast_episodes import PodcastEpisode
from app.schemas.podcast_shows import PodcastShow
from app.schemas.positions import Position
from app.schemas.seasons import Season
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo
from app.services.combine_score_service import grade_letter
from app.services.image_assets_service import get_current_image_urls_for_players
from app.services.news_service import get_trending_players


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

FEATURED_TARGET = 4
"""Maximum number of featured cards to render (the 2x2 grid on wide screens)."""

FEATURED_FLOOR = 2
"""If fewer than this number qualify, hide the featured row entirely."""

TRENDING_TOTAL_LIMIT = 10
"""Total players surfaced across both tiers."""

MIN_FEATURED_MENTIONS = 5
"""A featured card needs at least this many mentions in the window."""

TRENDING_WINDOW_DAYS = 7

RECENT_MENTIONS_PER_PLAYER = 2

HOT_RATIO = 1.5
"""(last 2-day mention rate) / (prior 5-day rate) at or above this -> 'hot'."""

COOLING_RATIO = 0.5
"""... at or below this -> 'cooling'."""


# ---------------------------------------------------------------------------
# Public response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrendingMentionPreview:
    """One recent content mention surfaced beneath a featured card."""

    title: str
    url: str
    source_name: str
    content_type: str  # "news" | "podcast" | "video"
    published_at: datetime


@dataclass(frozen=True, slots=True)
class TrendingStatLine:
    """Latest college production line for the featured stat strip."""

    season: str
    ppg: Optional[float]
    rpg: Optional[float]
    apg: Optional[float]
    spg: Optional[float]
    bpg: Optional[float]
    fg_pct: Optional[float]
    three_p_pct: Optional[float]
    ft_pct: Optional[float]


@dataclass(frozen=True, slots=True)
class FeaturedTrendingPlayer:
    """A trending player rendered as a rich featured card."""

    player_id: int
    rank: int
    display_name: str
    slug: str
    photo_url: str
    school: str
    position: str
    draft_year: Optional[int]
    mention_count: int
    daily_counts: list[int]
    spike_state: Optional[str]  # "hot" | "cooling" | None
    content_mix: dict[str, int]  # {"news": N, "podcast": N, "video": N}
    dominant_news_tag: Optional[str]
    combine_grade: Optional[str]
    latest_stats: TrendingStatLine
    recent_mentions: list[TrendingMentionPreview]
    latest_mention_at: Optional[datetime]


@dataclass(frozen=True, slots=True)
class CompactTrendingPlayer:
    """A trending player rendered as a smaller leaderboard row."""

    player_id: int
    rank: int
    display_name: str
    slug: str
    photo_url: Optional[str]
    school: Optional[str]
    position: Optional[str]
    draft_year: Optional[int]
    mention_count: int
    daily_counts: list[int]
    dominant_news_tag: Optional[str]


@dataclass(frozen=True, slots=True)
class ExpandedTrendingPlayers:
    """Two-tier homepage trending payload."""

    featured: list[FeaturedTrendingPlayer] = field(default_factory=list)
    compact: list[CompactTrendingPlayer] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.featured and not self.compact


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def get_expanded_trending_players(
    db: AsyncSession,
    *,
    days: int = TRENDING_WINDOW_DAYS,
    limit: int = TRENDING_TOTAL_LIMIT,
    image_style: str = "default",
) -> ExpandedTrendingPlayers:
    """Return a featured + compact split of the most-trending players.

    Walks the base trending list in mention-rank order, takes the first
    ``FEATURED_TARGET`` fully-populated players as featured, and assigns the
    rest to the compact tail. If fewer than ``FEATURED_FLOOR`` players qualify
    for the featured row it is hidden entirely and all 10 render as compact.
    """
    base = await get_trending_players(db, days=days, limit=limit)
    if not base:
        return ExpandedTrendingPlayers()

    player_ids = [tp.player_id for tp in base]

    # Batched supplementary lookups. AsyncSession does not support concurrent
    # queries on the same session, so these run sequentially.
    photos = await get_current_image_urls_for_players(
        db, player_ids=player_ids, style=image_style
    )
    statuses = await _load_player_statuses(db, player_ids)
    masters = await _load_player_masters(db, player_ids)
    college_stats = await _load_latest_college_stats(db, player_ids)
    combine_grades = await _load_combine_grades(db, player_ids, masters)
    content_mix = await _load_content_type_breakdown(db, player_ids, days)
    dominant_tags = await _load_dominant_news_tags(db, player_ids, days)
    recent_mentions = await _load_recent_mentions(
        db, player_ids, days, limit_per_player=RECENT_MENTIONS_PER_PLAYER
    )

    classifications: list[tuple[int, Any, bool]] = []
    for rank_idx, tp in enumerate(base, start=1):
        master = masters.get(tp.player_id, {})
        status = statuses.get(tp.player_id, {})
        latest = college_stats.get(tp.player_id)
        photo = photos.get(tp.player_id)

        is_eligible = (
            not master.get("is_stub", True)
            and photo is not None
            and status.get("position") is not None
            and master.get("school") is not None
            and latest is not None
            and latest.ppg is not None
            and tp.mention_count >= MIN_FEATURED_MENTIONS
        )
        classifications.append((rank_idx, tp, is_eligible))

    featured_pairs: list[tuple[int, Any]] = []
    compact_pairs: list[tuple[int, Any]] = []
    for rank_idx, tp, eligible in classifications:
        if eligible and len(featured_pairs) < FEATURED_TARGET:
            featured_pairs.append((rank_idx, tp))
        else:
            compact_pairs.append((rank_idx, tp))

    if len(featured_pairs) < FEATURED_FLOOR:
        featured_pairs = []
        compact_pairs = [(rank_idx, tp) for rank_idx, tp, _ in classifications]

    featured: list[FeaturedTrendingPlayer] = []
    for rank_idx, tp in featured_pairs:
        master = masters[tp.player_id]
        status = statuses[tp.player_id]
        latest = college_stats[tp.player_id]
        featured.append(
            FeaturedTrendingPlayer(
                player_id=tp.player_id,
                rank=rank_idx,
                display_name=tp.display_name,
                slug=tp.slug,
                photo_url=photos[tp.player_id],
                school=master["school"],
                position=status["position"],
                draft_year=master.get("draft_year"),
                mention_count=tp.mention_count,
                daily_counts=list(tp.daily_counts),
                spike_state=_compute_spike_state(tp.daily_counts),
                content_mix=content_mix.get(
                    tp.player_id, {"news": 0, "podcast": 0, "video": 0}
                ),
                dominant_news_tag=dominant_tags.get(tp.player_id),
                combine_grade=combine_grades.get(tp.player_id),
                latest_stats=latest,
                recent_mentions=recent_mentions.get(tp.player_id, []),
                latest_mention_at=tp.latest_mention_at,
            )
        )

    compact: list[CompactTrendingPlayer] = []
    for rank_idx, tp in compact_pairs:
        master = masters.get(tp.player_id, {})
        status = statuses.get(tp.player_id, {})
        compact.append(
            CompactTrendingPlayer(
                player_id=tp.player_id,
                rank=rank_idx,
                display_name=tp.display_name,
                slug=tp.slug,
                photo_url=photos.get(tp.player_id),
                school=master.get("school") or tp.school,
                position=status.get("position"),
                draft_year=master.get("draft_year"),
                mention_count=tp.mention_count,
                daily_counts=list(tp.daily_counts),
                dominant_news_tag=dominant_tags.get(tp.player_id),
            )
        )

    return ExpandedTrendingPlayers(featured=featured, compact=compact)


# ---------------------------------------------------------------------------
# Spike detection
# ---------------------------------------------------------------------------


def _compute_spike_state(daily_counts: list[int]) -> Optional[str]:
    """Classify the trailing daily-counts series as 'hot', 'cooling', or None.

    Compares the last 2-day mention rate against the prior 5-day rate. The
    series is oldest-first with one entry per day in the trending window.
    """
    if not daily_counts or len(daily_counts) < 7:
        return None

    last_2 = daily_counts[-2:]
    prior_5 = daily_counts[-7:-2]
    last_2_rate = sum(last_2) / 2.0
    prior_5_rate = sum(prior_5) / 5.0

    if prior_5_rate == 0:
        # Pure new-arrival case: only flag as hot if the trailing burst is real.
        return "hot" if sum(last_2) >= 3 else None

    ratio = last_2_rate / prior_5_rate
    if ratio >= HOT_RATIO:
        return "hot"
    if ratio <= COOLING_RATIO:
        return "cooling"
    return None


# ---------------------------------------------------------------------------
# Batched lookups
# ---------------------------------------------------------------------------


async def _load_player_masters(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, dict[str, Any]]:
    if not player_ids:
        return {}
    stmt = select(  # type: ignore[call-overload]
        PlayerMaster.id,
        PlayerMaster.is_stub,
        PlayerMaster.school,
        PlayerMaster.draft_year,
    ).where(cast(Any, PlayerMaster.id).in_(player_ids))
    result = await db.execute(stmt)
    return {
        int(row["id"]): {
            "is_stub": bool(row["is_stub"]),
            "school": row["school"],
            "draft_year": row["draft_year"],
        }
        for row in result.mappings().all()
        if row["id"] is not None
    }


async def _load_player_statuses(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Return position label, height, weight per player (best-effort)."""
    if not player_ids:
        return {}
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerStatus.player_id,
            PlayerStatus.raw_position,
            PlayerStatus.height_in,
            PlayerStatus.weight_lb,
            cast(Any, Position.code).label("position_code"),
        )
        .select_from(PlayerStatus)
        .outerjoin(Position, cast(Any, Position.id) == PlayerStatus.position_id)
        .where(cast(Any, PlayerStatus.player_id).in_(player_ids))
    )
    result = await db.execute(stmt)
    out: dict[int, dict[str, Any]] = {}
    for row in result.mappings().all():
        pid = int(row["player_id"]) if row["player_id"] is not None else None
        if pid is None:
            continue
        position = row["position_code"] or row["raw_position"]
        out[pid] = {
            "position": position,
            "height_in": row["height_in"],
            "weight_lb": row["weight_lb"],
        }
    return out


async def _load_latest_college_stats(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, TrendingStatLine]:
    """Return latest college season stat row per player (by season desc)."""
    if not player_ids:
        return {}

    rn_col = (
        func.row_number()
        .over(
            partition_by=cast(Any, PlayerCollegeStats.player_id),
            order_by=desc(cast(Any, PlayerCollegeStats.season)),
        )
        .label("rn")
    )
    inner = (
        select(  # type: ignore[call-overload, misc]
            PlayerCollegeStats.player_id,
            PlayerCollegeStats.season,
            PlayerCollegeStats.ppg,
            PlayerCollegeStats.rpg,
            PlayerCollegeStats.apg,
            PlayerCollegeStats.spg,
            PlayerCollegeStats.bpg,
            PlayerCollegeStats.fg_pct,
            PlayerCollegeStats.three_p_pct,
            PlayerCollegeStats.ft_pct,
            rn_col,
        )
        .where(cast(Any, PlayerCollegeStats.player_id).in_(player_ids))
        .subquery()
    )
    stmt = select(inner).where(inner.c.rn == 1)
    result = await db.execute(stmt)
    out: dict[int, TrendingStatLine] = {}
    for row in result.mappings().all():
        pid = int(row["player_id"]) if row["player_id"] is not None else None
        if pid is None:
            continue
        out[pid] = TrendingStatLine(
            season=str(row["season"]),
            ppg=row["ppg"],
            rpg=row["rpg"],
            apg=row["apg"],
            spg=row["spg"],
            bpg=row["bpg"],
            fg_pct=row["fg_pct"],
            three_p_pct=row["three_p_pct"],
            ft_pct=row["ft_pct"],
        )
    return out


async def _load_combine_grades(
    db: AsyncSession,
    player_ids: list[int],
    masters: dict[int, dict[str, Any]],
) -> dict[int, str]:
    """Return letter grades for each player who has a current combine snapshot.

    Pulls the player's overall combine-score percentile from the snapshot
    matching their draft year (mapped via ``Season.start_year``) and converts
    it to a compact letter via ``grade_letter``. Players without a draft year
    or matching snapshot simply omit the pill on the card.
    """
    if not player_ids:
        return {}

    draft_years_set: set[int] = set()
    for m in masters.values():
        dy = m.get("draft_year")
        if dy is not None:
            draft_years_set.add(int(dy))
    if not draft_years_set:
        return {}

    season_stmt = select(Season.id, Season.start_year).where(  # type: ignore[call-overload]
        cast(Any, Season.start_year).in_(sorted(draft_years_set))
    )
    season_rows = await db.execute(season_stmt)
    season_id_by_year: dict[int, int] = {
        int(row["start_year"]): int(row["id"])
        for row in season_rows.mappings().all()
        if row["id"] is not None and row["start_year"] is not None
    }
    if not season_id_by_year:
        return {}

    snap_stmt = select(MetricSnapshot.id, MetricSnapshot.season_id).where(  # type: ignore[call-overload]
        cast(Any, MetricSnapshot.source) == MetricSource.combine_score,
        cast(Any, MetricSnapshot.cohort) == CohortType.current_draft,
        cast(Any, MetricSnapshot.is_current).is_(True),
        cast(Any, MetricSnapshot.season_id).in_(list(season_id_by_year.values())),
        cast(Any, MetricSnapshot.position_scope_parent).is_(None),
        cast(Any, MetricSnapshot.position_scope_fine).is_(None),
    )
    snap_rows = await db.execute(snap_stmt)
    snapshot_id_by_season: dict[int, int] = {}
    for row in snap_rows.mappings().all():
        sid = row["season_id"]
        if sid is None:
            continue
        snapshot_id_by_season[int(sid)] = int(row["id"])
    if not snapshot_id_by_season:
        return {}

    # Pin each player to *their* draft-year snapshot. Players without a
    # draft_year (or whose draft_year has no current snapshot) drop out
    # here and will not receive a grade — this is intentional. The pairing
    # is also enforced post-query so a stale PMV row in a different season's
    # snapshot can't leak into the wrong player's grade.
    snapshot_for_player: dict[int, int] = {}
    for pid, master in masters.items():
        dy = master.get("draft_year")
        if dy is None:
            continue
        season_id = season_id_by_year.get(int(dy))
        if season_id is None:
            continue
        snapshot_id = snapshot_id_by_season.get(season_id)
        if snapshot_id is None:
            continue
        snapshot_for_player[pid] = snapshot_id
    if not snapshot_for_player:
        return {}

    defn_stmt = select(MetricDefinition.id).where(  # type: ignore[call-overload]
        cast(Any, MetricDefinition.metric_key) == "combine_score_overall"
    )
    defn_result = await db.execute(defn_stmt)
    overall_def_id = defn_result.scalar_one_or_none()
    if overall_def_id is None:
        return {}

    pmv_stmt = select(  # type: ignore[call-overload]
        PlayerMetricValue.player_id,
        PlayerMetricValue.snapshot_id,
        PlayerMetricValue.percentile,
    ).where(
        cast(Any, PlayerMetricValue.player_id).in_(list(snapshot_for_player.keys())),
        cast(Any, PlayerMetricValue.snapshot_id).in_(
            list(set(snapshot_for_player.values()))
        ),
        cast(Any, PlayerMetricValue.metric_definition_id) == overall_def_id,
    )
    pmv_rows = await db.execute(pmv_stmt)

    out: dict[int, str] = {}
    for row in pmv_rows.mappings().all():
        raw_pid = row["player_id"]
        raw_sid = row["snapshot_id"]
        if raw_pid is None or raw_sid is None:
            continue
        pid = int(raw_pid)
        sid = int(raw_sid)
        # Enforce the pairing: this PMV row must come from the player's own
        # draft-year snapshot, not just *any* snapshot in scope.
        if snapshot_for_player.get(pid) != sid:
            continue
        pct = row["percentile"]
        letter = grade_letter(float(pct) if pct is not None else None)
        if letter is not None:
            out[pid] = letter
    return out


async def _load_content_type_breakdown(
    db: AsyncSession, player_ids: list[int], days: int
) -> dict[int, dict[str, int]]:
    """Return per-player counts of mentions by content type within the window."""
    if not player_ids:
        return {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerContentMention.player_id,
            PlayerContentMention.content_type,
            func.count().label("c"),
        )
        .where(cast(Any, PlayerContentMention.player_id).in_(player_ids))
        .where(cast(Any, PlayerContentMention.published_at) >= cutoff)
        .group_by(PlayerContentMention.player_id, PlayerContentMention.content_type)
    )
    result = await db.execute(stmt)
    out: dict[int, dict[str, int]] = {}
    for row in result.mappings().all():
        pid = int(row["player_id"]) if row["player_id"] is not None else None
        if pid is None:
            continue
        bucket = out.setdefault(pid, {"news": 0, "podcast": 0, "video": 0})
        ctype = row["content_type"]
        if ctype == ContentType.NEWS:
            bucket["news"] = int(row["c"])
        elif ctype == ContentType.PODCAST:
            bucket["podcast"] = int(row["c"])
        elif ctype == ContentType.VIDEO:
            bucket["video"] = int(row["c"])
    return out


async def _load_dominant_news_tags(
    db: AsyncSession, player_ids: list[int], days: int
) -> dict[int, str]:
    """Return the most-frequent NewsItem.tag per player within the window.

    News-only by design; podcast and video tags are intentionally excluded
    since they live in different enums.
    """
    if not player_ids:
        return {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerContentMention.player_id,
            NewsItem.tag,
            func.count().label("c"),
        )
        .join(NewsItem, cast(Any, NewsItem.id) == PlayerContentMention.content_id)
        .where(cast(Any, PlayerContentMention.player_id).in_(player_ids))
        .where(cast(Any, PlayerContentMention.content_type) == ContentType.NEWS)
        .where(cast(Any, PlayerContentMention.published_at) >= cutoff)
        .group_by(PlayerContentMention.player_id, NewsItem.tag)
    )
    result = await db.execute(stmt)
    counts_by_player: dict[int, dict[str, int]] = {}
    for row in result.mappings().all():
        pid = int(row["player_id"]) if row["player_id"] is not None else None
        if pid is None:
            continue
        tag_value = _resolve_news_tag_value(row["tag"])
        bucket = counts_by_player.setdefault(pid, {})
        bucket[tag_value] = bucket.get(tag_value, 0) + int(row["c"])
    out: dict[int, str] = {}
    for pid, tag_counts in counts_by_player.items():
        if not tag_counts:
            continue
        # Highest count wins; tie-break alphabetically for determinism.
        best = max(tag_counts.items(), key=lambda kv: (kv[1], kv[0]))
        out[pid] = best[0]
    return out


def _resolve_news_tag_value(raw: Any) -> str:
    """Normalize a NewsItemTag (enum or stringly-typed) to its display value."""
    if isinstance(raw, NewsItemTag):
        return raw.value
    if isinstance(raw, str):
        try:
            return NewsItemTag(raw).value
        except ValueError:
            try:
                return NewsItemTag[raw].value
            except KeyError:
                return raw
    return str(raw)


async def _load_recent_mentions(
    db: AsyncSession,
    player_ids: list[int],
    days: int,
    *,
    limit_per_player: int = RECENT_MENTIONS_PER_PLAYER,
) -> dict[int, list[TrendingMentionPreview]]:
    """Return the top-N most-recent mention previews per player.

    Three separate top-N-per-player queries (news / podcast / video) are
    unioned and re-trimmed to ``limit_per_player`` per player in Python. This
    avoids a single mega-join across three polymorphic content tables.
    """
    if not player_ids:
        return {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    candidates_by_player: dict[int, list[TrendingMentionPreview]] = {}

    def _accept(pid: int, preview: TrendingMentionPreview) -> None:
        candidates_by_player.setdefault(pid, []).append(preview)

    rn_partition = cast(Any, PlayerContentMention.player_id)
    rn_order = desc(cast(Any, PlayerContentMention.published_at))

    # --- News ---
    rn_news = (
        func.row_number().over(partition_by=rn_partition, order_by=rn_order).label("rn")
    )
    news_inner = (
        select(
            cast(Any, PlayerContentMention.player_id).label("player_id"),
            cast(Any, NewsItem.title).label("title"),
            cast(Any, NewsItem.url).label("url"),
            cast(Any, NewsSource.display_name).label("source_name"),
            cast(Any, PlayerContentMention.published_at).label("published_at"),
            literal("news").label("content_type"),
            rn_news,
        )
        .join(NewsItem, cast(Any, NewsItem.id) == PlayerContentMention.content_id)
        .join(NewsSource, cast(Any, NewsSource.id) == NewsItem.source_id)
        .where(cast(Any, PlayerContentMention.player_id).in_(player_ids))
        .where(cast(Any, PlayerContentMention.content_type) == ContentType.NEWS)
        .where(cast(Any, PlayerContentMention.published_at) >= cutoff)
        .subquery()
    )
    news_stmt = select(news_inner).where(news_inner.c.rn <= limit_per_player)
    news_rows = await db.execute(news_stmt)
    for row in news_rows.mappings().all():
        _accept(
            int(row["player_id"]),
            TrendingMentionPreview(
                title=str(row["title"] or ""),
                url=str(row["url"] or ""),
                source_name=str(row["source_name"] or ""),
                content_type="news",
                published_at=row["published_at"],
            ),
        )

    # --- Podcasts ---
    rn_pod = (
        func.row_number().over(partition_by=rn_partition, order_by=rn_order).label("rn")
    )
    pod_inner = (
        select(
            cast(Any, PlayerContentMention.player_id).label("player_id"),
            cast(Any, PodcastEpisode.title).label("title"),
            func.coalesce(
                cast(Any, PodcastEpisode.episode_url),
                cast(Any, PodcastEpisode.audio_url),
            ).label("url"),
            cast(Any, PodcastShow.display_name).label("source_name"),
            cast(Any, PlayerContentMention.published_at).label("published_at"),
            literal("podcast").label("content_type"),
            rn_pod,
        )
        .join(
            PodcastEpisode,
            cast(Any, PodcastEpisode.id) == PlayerContentMention.content_id,
        )
        .join(PodcastShow, cast(Any, PodcastShow.id) == PodcastEpisode.show_id)
        .where(cast(Any, PlayerContentMention.player_id).in_(player_ids))
        .where(cast(Any, PlayerContentMention.content_type) == ContentType.PODCAST)
        .where(cast(Any, PlayerContentMention.published_at) >= cutoff)
        .subquery()
    )
    pod_stmt = select(pod_inner).where(pod_inner.c.rn <= limit_per_player)
    pod_rows = await db.execute(pod_stmt)
    for row in pod_rows.mappings().all():
        _accept(
            int(row["player_id"]),
            TrendingMentionPreview(
                title=str(row["title"] or ""),
                url=str(row["url"] or ""),
                source_name=str(row["source_name"] or ""),
                content_type="podcast",
                published_at=row["published_at"],
            ),
        )

    # --- Videos ---
    rn_vid = (
        func.row_number().over(partition_by=rn_partition, order_by=rn_order).label("rn")
    )
    vid_inner = (
        select(
            cast(Any, PlayerContentMention.player_id).label("player_id"),
            cast(Any, YouTubeVideo.title).label("title"),
            cast(Any, YouTubeVideo.youtube_url).label("url"),
            cast(Any, YouTubeChannel.display_name).label("source_name"),
            cast(Any, PlayerContentMention.published_at).label("published_at"),
            literal("video").label("content_type"),
            rn_vid,
        )
        .join(
            YouTubeVideo,
            cast(Any, YouTubeVideo.id) == PlayerContentMention.content_id,
        )
        .join(
            YouTubeChannel,
            cast(Any, YouTubeChannel.id) == YouTubeVideo.channel_id,
        )
        .where(cast(Any, PlayerContentMention.player_id).in_(player_ids))
        .where(cast(Any, PlayerContentMention.content_type) == ContentType.VIDEO)
        .where(cast(Any, PlayerContentMention.published_at) >= cutoff)
        .subquery()
    )
    vid_stmt = select(vid_inner).where(vid_inner.c.rn <= limit_per_player)
    vid_rows = await db.execute(vid_stmt)
    for row in vid_rows.mappings().all():
        _accept(
            int(row["player_id"]),
            TrendingMentionPreview(
                title=str(row["title"] or ""),
                url=str(row["url"] or ""),
                source_name=str(row["source_name"] or ""),
                content_type="video",
                published_at=row["published_at"],
            ),
        )

    # Trim each player's pool to the most-recent ``limit_per_player`` after merging.
    result: dict[int, list[TrendingMentionPreview]] = {}
    for pid, previews in candidates_by_player.items():
        previews.sort(key=lambda p: p.published_at, reverse=True)
        result[pid] = previews[:limit_per_player]
    return result

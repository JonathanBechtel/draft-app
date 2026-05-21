"""Per-angle data assembly for the gather CLI.

Each ``gather_*`` function returns a populated GatherResult. Image generation
is delegated to :mod:`app.services.x_threads.image_builder`, which writes PNGs
to disk and returns their paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricCategory, SimilarityDimension
from app.schemas.news_items import NewsItem
from app.services.metrics_service import get_player_metrics
from app.services.similarity_service import get_similar_players

from .image_builder import (
    render_h2h_share_card,
    render_outlier_card,
    render_performance_share_card,
)
from .outlier_finder import find_outlier_candidate
from .types import AnglePick, CompFact, GatherResult, StatFact


_TOP_METRIC_LIMIT = 5
_TOP_COMP_LIMIT = 3


async def gather_for_pick(
    db: AsyncSession,
    pick: AnglePick,
    *,
    output_dir: Path,
) -> GatherResult:
    """Dispatch to the right gather function for the chosen angle."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if pick.angle == "spotlight":
        return await _gather_spotlight(db, pick, output_dir)
    if pick.angle == "h2h":
        return await _gather_h2h(db, pick, output_dir)
    if pick.angle == "outlier":
        return await _gather_outlier(db, pick, output_dir)
    if pick.angle == "news_tag":
        return await _gather_news_tag(db, pick, output_dir)
    raise ValueError(f"Unsupported angle: {pick.angle}")


async def _top_facts(
    db: AsyncSession, slug: str, limit: int = _TOP_METRIC_LIMIT
) -> list[StatFact]:
    """Pull the player's top percentile metrics across all categories."""
    rows: list[StatFact] = []
    for category in (
        MetricCategory.anthropometrics,
        MetricCategory.combine_performance,
        MetricCategory.shooting,
    ):
        try:
            payload = await get_player_metrics(
                db,
                slug=slug,
                cohort=CohortType.current_draft,
                category=category,
                position_adjusted=False,
            )
        except ValueError:
            continue
        for metric in payload.get("metrics", []):
            if metric.get("percentile") is None:
                continue
            value = metric.get("value")
            unit = metric.get("unit") or ""
            display_value = f"{value}{unit}" if value is not None else "—"
            rows.append(
                StatFact(
                    label=str(metric.get("metric")),
                    value=display_value,
                    percentile=float(metric["percentile"]),
                    rank=metric.get("rank"),
                    population_size=metric.get("population_size"),
                )
            )
    rows.sort(key=lambda f: f.percentile or 0, reverse=True)
    return rows[:limit]


async def _top_comps(
    db: AsyncSession, slug: str, limit: int = _TOP_COMP_LIMIT
) -> list[CompFact]:
    try:
        payload = await get_similar_players(
            db,
            slug=slug,
            dimension=SimilarityDimension.anthro,
            same_position=False,
            nba_only=False,
            limit=limit,
        )
    except ValueError:
        return []
    return [
        CompFact(
            slug=str(p.get("slug") or ""),
            display_name=str(p.get("display_name") or ""),
            school=p.get("school"),
            similarity_score=float(p.get("similarity_score") or 0.0),
        )
        for p in payload.get("players", [])
    ]


async def _gather_spotlight(
    db: AsyncSession, pick: AnglePick, output_dir: Path
) -> GatherResult:
    player = pick.players[0]
    facts = await _top_facts(db, player.slug)
    comps = await _top_comps(db, player.slug)
    headline = f"Spotlight: {player.display_name}"

    images: list[str] = []
    card_path = await render_performance_share_card(
        db, player_id=player.id, output_dir=output_dir
    )
    if card_path is not None:
        images.append(str(card_path))

    return GatherResult(
        angle=pick.angle,
        headline=headline,
        players=pick.players,
        facts=facts,
        comps=comps,
        images=images,
        notes=pick.notes,
    )


async def _gather_h2h(
    db: AsyncSession, pick: AnglePick, output_dir: Path
) -> GatherResult:
    p1, p2 = pick.players[0], pick.players[1]
    p1_facts = await _top_facts(db, p1.slug, limit=3)
    p2_facts = await _top_facts(db, p2.slug, limit=3)
    headline = f"H2H: {p1.display_name} vs {p2.display_name}"

    images: list[str] = []
    card_path = await render_h2h_share_card(
        db, player_ids=[p1.id, p2.id], output_dir=output_dir
    )
    if card_path is not None:
        images.append(str(card_path))

    return GatherResult(
        angle=pick.angle,
        headline=headline,
        players=pick.players,
        facts=p1_facts + p2_facts,
        images=images,
        extra={
            "player_facts": {
                str(p1.id): [f.__dict__ for f in p1_facts],
                str(p2.id): [f.__dict__ for f in p2_facts],
            }
        },
        notes=pick.notes,
    )


async def _gather_outlier(
    db: AsyncSession, pick: AnglePick, output_dir: Path
) -> GatherResult:
    excluded: set[int] = set()
    outlier = await find_outlier_candidate(db, excluded_player_ids=excluded)
    if outlier is None or outlier.player.id != pick.players[0].id:
        # The picker chose this player based on the same finder so this should
        # normally hit; rebuild with the player from the pick if it doesn't.
        outlier = await find_outlier_candidate(db, excluded_player_ids=excluded)
    if outlier is None:
        raise ValueError("outlier_finder returned no candidate")

    images: list[str] = []
    card_path = await render_outlier_card(outlier=outlier, output_dir=output_dir)
    if card_path is not None:
        images.append(str(card_path))

    # Also add the performance share card for richer images on the thread.
    perf_path = await render_performance_share_card(
        db, player_id=outlier.player.id, output_dir=output_dir
    )
    if perf_path is not None:
        images.append(str(perf_path))

    return GatherResult(
        angle=pick.angle,
        headline=outlier.headline,
        players=[outlier.player],
        facts=outlier.stats,
        images=images,
        notes=outlier.support_text,
        extra={"subtype": outlier.subtype},
    )


async def _gather_news_tag(
    db: AsyncSession, pick: AnglePick, output_dir: Path
) -> GatherResult:
    assert pick.news_item_id is not None
    stmt = select(NewsItem).where(NewsItem.id == pick.news_item_id)  # type: ignore[arg-type]
    result = await db.execute(stmt)
    news: Optional[NewsItem] = result.scalar_one_or_none()
    if news is None:
        raise ValueError("news_item_not_found")

    player = pick.players[0]
    facts = await _top_facts(db, player.slug, limit=3)
    headline = f"News reaction: {news.title}"

    images: list[str] = []
    perf_path = await render_performance_share_card(
        db, player_id=player.id, output_dir=output_dir
    )
    if perf_path is not None:
        images.append(str(perf_path))

    return GatherResult(
        angle=pick.angle,
        headline=headline,
        players=pick.players,
        facts=facts,
        images=images,
        news={
            "id": news.id,
            "title": news.title,
            "summary": news.summary,
            "url": news.url,
            "published_at": news.published_at.isoformat()
            if news.published_at
            else None,
            "tag": news.tag.value if news.tag else None,
            "source_id": news.source_id,
        },
        notes=pick.notes,
    )

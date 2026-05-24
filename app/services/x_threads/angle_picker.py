"""Pick an angle + subject for the next X thread.

Dedup window prevents reposting the same player/news under the same angle in
the recent past. The picker is intentionally simple: it walks a shuffled list
of viable angles and returns the first one with a fresh subject.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Optional, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.combine_anthro import CombineAnthro
from app.schemas.news_items import NewsItem
from app.schemas.players_master import PlayerMaster
from app.schemas.x_post_history import XPostAngle, XPostHistory

from .outlier_finder import find_outlier_candidate
from .types import AnglePick, PlayerCard

DEFAULT_DEDUP_DAYS = 14


_ANGLE_ORDER = [
    XPostAngle.spotlight,
    XPostAngle.outlier,
    XPostAngle.h2h,
    XPostAngle.news_tag,
]


async def _recently_used_player_ids(
    db: AsyncSession,
    angle: XPostAngle,
    window_days: int,
) -> set[int]:
    """Return player IDs used by *any* post under this angle in the window.

    Tweets reference players via the JSONB `player_ids` array, so we expand
    that array in Postgres before pulling the distinct set back into Python.
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    stmt: Any = (
        select(  # type: ignore[call-overload, misc]
            func.jsonb_array_elements_text(XPostHistory.player_ids).label("pid"),
        )
        .where(XPostHistory.angle == angle)  # type: ignore[arg-type]
        .where(XPostHistory.created_at >= cutoff)  # type: ignore[arg-type]
    )
    result = await db.execute(stmt)
    return {int(row.pid) for row in result.all() if row.pid is not None}


async def _recently_used_news_ids(db: AsyncSession, window_days: int) -> set[int]:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    stmt: Any = (
        select(XPostHistory.news_item_id)  # type: ignore[call-overload]
        .where(XPostHistory.angle == XPostAngle.news_tag)  # type: ignore[arg-type]
        .where(XPostHistory.created_at >= cutoff)  # type: ignore[arg-type]
        .where(XPostHistory.news_item_id.is_not(None))  # type: ignore[union-attr]
    )
    result = await db.execute(stmt)
    return {row[0] for row in result.all() if row[0] is not None}


async def _latest_draft_year(db: AsyncSession) -> Optional[int]:
    """Latest draft year that has at least one player with combine anthro data.

    PlayerMaster has stub rows for far-future draft classes; without the join
    those make ``max(draft_year)`` useless for picking real prospects.
    """
    stmt = (
        select(func.max(PlayerMaster.draft_year))  # type: ignore[arg-type]
        .select_from(PlayerMaster)
        .join(CombineAnthro, CombineAnthro.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .where(PlayerMaster.is_stub.is_(False))  # type: ignore[attr-defined]
    )
    result = await db.execute(stmt)
    return result.scalar()


async def _player_to_card(db: AsyncSession, player_id: int) -> Optional[PlayerCard]:
    stmt: Any = select(  # type: ignore[call-overload, misc]
        PlayerMaster.id,
        PlayerMaster.slug,
        PlayerMaster.display_name,
        PlayerMaster.school,
        PlayerMaster.draft_year,
    ).where(PlayerMaster.id == player_id)  # type: ignore[arg-type]
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    return PlayerCard(
        id=row.id,
        slug=row.slug or "",
        display_name=row.display_name or "",
        school=row.school,
        draft_year=row.draft_year,
    )


async def _pick_spotlight(db: AsyncSession, window_days: int) -> Optional[AnglePick]:
    """Pick a current-class player with combine data who hasn't been spotlighted recently."""
    draft_year = await _latest_draft_year(db)
    if draft_year is None:
        return None

    used = await _recently_used_player_ids(db, XPostAngle.spotlight, window_days)

    stmt: Any = (
        select(  # type: ignore[call-overload, misc]
            PlayerMaster.id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            PlayerMaster.school,
            PlayerMaster.draft_year,
        )
        .join(CombineAnthro, CombineAnthro.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .where(PlayerMaster.is_stub.is_(False))  # type: ignore[attr-defined]
        .where(PlayerMaster.draft_year == draft_year)  # type: ignore[arg-type]
        .where(PlayerMaster.slug.is_not(None))  # type: ignore[union-attr]
    )
    if used:
        stmt = stmt.where(PlayerMaster.id.notin_(used))  # type: ignore[union-attr]
    stmt = stmt.order_by(func.random()).limit(1)

    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    player = PlayerCard(
        id=row.id,
        slug=row.slug,
        display_name=row.display_name or "",
        school=row.school,
        draft_year=row.draft_year,
    )
    return AnglePick(
        angle=XPostAngle.spotlight.value,
        players=[player],
        notes=f"Current-class spotlight pick for {player.display_name}.",
    )


async def _pick_h2h(db: AsyncSession, window_days: int) -> Optional[AnglePick]:
    """Pick two current-class players with combine data, not paired recently."""
    draft_year = await _latest_draft_year(db)
    if draft_year is None:
        return None

    used = await _recently_used_player_ids(db, XPostAngle.h2h, window_days)

    stmt: Any = (
        select(  # type: ignore[call-overload, misc]
            PlayerMaster.id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            PlayerMaster.school,
            PlayerMaster.draft_year,
        )
        .join(CombineAnthro, CombineAnthro.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .where(PlayerMaster.is_stub.is_(False))  # type: ignore[attr-defined]
        .where(PlayerMaster.draft_year == draft_year)  # type: ignore[arg-type]
        .where(PlayerMaster.slug.is_not(None))  # type: ignore[union-attr]
    )
    if used:
        stmt = stmt.where(PlayerMaster.id.notin_(used))  # type: ignore[union-attr]
    stmt = stmt.order_by(func.random()).limit(2)

    result = await db.execute(stmt)
    rows = result.all()
    if len(rows) < 2:
        return None

    players = [
        PlayerCard(
            id=row.id,
            slug=row.slug,
            display_name=row.display_name or "",
            school=row.school,
            draft_year=row.draft_year,
        )
        for row in rows
    ]
    return AnglePick(
        angle=XPostAngle.h2h.value,
        players=players,
        notes=f"H2H pairing: {players[0].display_name} vs {players[1].display_name}.",
    )


async def _pick_outlier(db: AsyncSession, window_days: int) -> Optional[AnglePick]:
    used = await _recently_used_player_ids(db, XPostAngle.outlier, window_days)
    outlier = await find_outlier_candidate(db, excluded_player_ids=used)
    if outlier is None:
        return None
    return AnglePick(
        angle=XPostAngle.outlier.value,
        players=[outlier.player],
        notes=outlier.support_text,
    )


async def _pick_news_tag(db: AsyncSession, window_days: int) -> Optional[AnglePick]:
    used_news = await _recently_used_news_ids(db, window_days)
    cutoff = datetime.utcnow() - timedelta(days=3)

    stmt: Any = (
        select(NewsItem.id, NewsItem.player_id)  # type: ignore[call-overload]
        .where(NewsItem.player_id.is_not(None))  # type: ignore[union-attr]
        .where(NewsItem.published_at >= cutoff)  # type: ignore[arg-type]
    )
    if used_news:
        stmt = stmt.where(NewsItem.id.notin_(used_news))  # type: ignore[union-attr]
    stmt = stmt.order_by(NewsItem.published_at.desc()).limit(25)  # type: ignore[attr-defined]

    result = await db.execute(stmt)
    rows = cast(list[Any], result.all())
    if not rows:
        return None

    chosen = random.choice(rows)
    player = await _player_to_card(db, int(chosen.player_id))
    if player is None:
        return None

    return AnglePick(
        angle=XPostAngle.news_tag.value,
        players=[player],
        news_item_id=int(chosen.id),
        notes=f"News reaction for {player.display_name}.",
    )


_PICKERS = {
    XPostAngle.spotlight: _pick_spotlight,
    XPostAngle.h2h: _pick_h2h,
    XPostAngle.outlier: _pick_outlier,
    XPostAngle.news_tag: _pick_news_tag,
}


async def pick_angle(
    db: AsyncSession,
    *,
    window_days: int = DEFAULT_DEDUP_DAYS,
    preferred_angle: Optional[XPostAngle] = None,
) -> Optional[AnglePick]:
    """Pick the next viable angle + subject.

    Args:
        db: Database session.
        window_days: Days to look back when deduping subjects.
        preferred_angle: Force a specific angle (skill override); fall back to
            other angles only if this one has no viable subject.

    Returns:
        AnglePick with chosen angle and subject(s), or None if nothing is viable.
    """
    if preferred_angle is not None:
        picker = _PICKERS[preferred_angle]
        pick = await picker(db, window_days)
        if pick is not None:
            return pick

    order = list(_ANGLE_ORDER)
    random.shuffle(order)
    for angle in order:
        pick = await _PICKERS[angle](db, window_days)
        if pick is not None:
            return pick
    return None

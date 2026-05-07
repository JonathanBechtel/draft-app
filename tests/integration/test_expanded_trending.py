"""Integration tests for the expanded trending-players service.

These tests exercise the eligibility gates and tier-split logic that
``get_expanded_trending_players`` layers on top of the base trending feed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType
from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot
from app.schemas.news_items import NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.player_content_mentions import (
    ContentType,
    MentionSource,
    PlayerContentMention,
)
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.services.expanded_trending_service import (
    FEATURED_TARGET,
    MIN_FEATURED_MENTIONS,
    get_expanded_trending_players,
)
from tests.integration.conftest import make_article, make_player


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _ensure_position(db: AsyncSession, code: str) -> Position:
    existing = await db.execute(
        select(Position).where(Position.code == code)  # type: ignore[arg-type]
    )
    pos = existing.scalars().first()
    if pos is not None:
        return pos
    pos = Position(code=code)
    db.add(pos)
    await db.flush()
    return pos


async def _ensure_image_snapshot(db: AsyncSession) -> PlayerImageSnapshot:
    existing = await db.execute(
        select(PlayerImageSnapshot).where(
            PlayerImageSnapshot.run_key == "test_run",  # type: ignore[arg-type]
            PlayerImageSnapshot.style == "default",  # type: ignore[arg-type]
        )
    )
    snap = existing.scalars().first()
    if snap is not None:
        return snap
    snap = PlayerImageSnapshot(
        run_key="test_run",
        version=1,
        is_current=True,
        style="default",
        cohort=CohortType.current_draft,
        image_size="1K",
        system_prompt="test prompt",
    )
    db.add(snap)
    await db.flush()
    return snap


async def _seed_player(
    db: AsyncSession,
    news_source: NewsSource,
    *,
    first_name: str,
    last_name: str = "Player",
    school: Optional[str] = "Test University",
    position_code: Optional[str] = "PG",
    ppg: Optional[float] = 18.0,
    is_stub: bool = False,
    mention_count: int = MIN_FEATURED_MENTIONS + 1,
    has_photo: bool = True,
    article_tag: NewsItemTag = NewsItemTag.MOCK_DRAFT,
) -> PlayerMaster:
    """Seed a player with knobs to break individual eligibility gates."""
    player = make_player(first_name, last_name, school=school)
    player.is_stub = is_stub
    db.add(player)
    await db.flush()
    pid = player.id
    assert pid is not None

    if position_code is not None:
        position = await _ensure_position(db, position_code)
        db.add(PlayerStatus(player_id=pid, position_id=position.id))

    if ppg is not None:
        db.add(
            PlayerCollegeStats(
                player_id=pid,
                season="2024-25",
                games=30,
                mpg=32.0,
                ppg=ppg,
                rpg=5.5,
                apg=3.4,
                fg_pct=46.2,
                three_p_pct=37.5,
                ft_pct=78.0,
            )
        )

    if has_photo:
        snap = await _ensure_image_snapshot(db)
        db.add(
            PlayerImageAsset(
                snapshot_id=snap.id,
                player_id=pid,
                s3_key=f"players/{pid}_{first_name.lower()}_default.png",
                public_url=f"https://cdn.test/{pid}.png",
                user_prompt="test",
            )
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(mention_count):
        article = make_article(
            news_source.id,  # type: ignore[arg-type]
            f"art-{pid}-{i}",
            hours_ago=i,
        )
        article.tag = article_tag
        db.add(article)
        await db.flush()
        db.add(
            PlayerContentMention(
                content_type=ContentType.NEWS,
                content_id=article.id,  # type: ignore[arg-type]
                player_id=pid,
                published_at=article.published_at,
                source=MentionSource.AI,
                created_at=now,
            )
        )

    await db.flush()
    return player


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetExpandedTrendingPlayers:
    """Eligibility gates and tier-split logic."""

    async def test_empty_when_no_trending_data(
        self, db_session: AsyncSession
    ) -> None:
        """No mentions in the window -> empty featured + compact lists."""
        result = await get_expanded_trending_players(db_session)
        assert result.featured == []
        assert result.compact == []
        assert result.is_empty

    async def test_fully_populated_player_lands_in_featured(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """A player satisfying every required gate must show up as featured."""
        await _seed_player(
            db_session, news_source, first_name="Cooper", last_name="Flagg"
        )
        # Need at least FEATURED_FLOOR (2) eligible to keep the featured row.
        await _seed_player(
            db_session, news_source, first_name="Dylan", last_name="Harper"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        assert len(result.featured) == 2
        assert {p.display_name for p in result.featured} == {
            "Cooper Flagg",
            "Dylan Harper",
        }
        flagg = next(p for p in result.featured if p.display_name == "Cooper Flagg")
        assert flagg.position == "PG"
        assert flagg.school == "Test University"
        assert flagg.photo_url.startswith("https://cdn.test/")
        assert flagg.latest_stats.ppg == pytest.approx(18.0)
        assert flagg.dominant_news_tag == NewsItemTag.MOCK_DRAFT.value
        assert flagg.content_mix["news"] >= MIN_FEATURED_MENTIONS

    async def test_missing_photo_demotes_to_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """No PlayerImageAsset row -> player must not be featured."""
        await _seed_player(
            db_session,
            news_source,
            first_name="NoPhoto",
            has_photo=False,
        )
        # Two eligible companions so the floor rule doesn't override.
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="One"
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="Two"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        featured_names = {p.display_name for p in result.featured}
        compact_names = {p.display_name for p in result.compact}
        assert "NoPhoto Player" not in featured_names
        assert "NoPhoto Player" in compact_names

    async def test_missing_college_ppg_demotes_to_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """No latest college-stats row with PPG -> demoted."""
        await _seed_player(
            db_session,
            news_source,
            first_name="NoStats",
            ppg=None,  # skip seeding the college stats row entirely
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="One"
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="Two"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        compact_names = {p.display_name for p in result.compact}
        assert "NoStats Player" in compact_names

    async def test_missing_position_demotes_to_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """No PlayerStatus / position row -> demoted."""
        await _seed_player(
            db_session,
            news_source,
            first_name="NoPos",
            position_code=None,
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="One"
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="Two"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        compact_names = {p.display_name for p in result.compact}
        assert "NoPos Player" in compact_names

    async def test_missing_school_demotes_to_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Empty school field on PlayerMaster -> demoted."""
        await _seed_player(
            db_session, news_source, first_name="NoSchool", school=None
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="One"
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="Two"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        compact_names = {p.display_name for p in result.compact}
        assert "NoSchool Player" in compact_names

    async def test_stub_player_demotes_to_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """is_stub=True -> demoted regardless of other fields."""
        # is_stub players also need a real last_name to pass the base
        # high-quality filter inside get_trending_players.
        await _seed_player(
            db_session, news_source, first_name="Stubbed", last_name="Stub", is_stub=True
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="One"
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="Two"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        featured_names = {p.display_name for p in result.featured}
        assert "Stubbed Stub" not in featured_names

    async def test_insufficient_mentions_demotes_to_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Below MIN_FEATURED_MENTIONS -> demoted even if everything else is fine."""
        await _seed_player(
            db_session,
            news_source,
            first_name="QuietBuzz",
            mention_count=MIN_FEATURED_MENTIONS - 1,
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="One"
        )
        await _seed_player(
            db_session, news_source, first_name="Eligible", last_name="Two"
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        featured_names = {p.display_name for p in result.featured}
        compact_names = {p.display_name for p in result.compact}
        assert "QuietBuzz Player" not in featured_names
        assert "QuietBuzz Player" in compact_names

    async def test_floor_rule_demotes_all_when_too_few_qualify(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Fewer than FEATURED_FLOOR eligible -> hide featured row entirely."""
        # One eligible player; one obviously demoted player to keep some volume.
        await _seed_player(
            db_session, news_source, first_name="OnlyOne", last_name="Eligible"
        )
        await _seed_player(
            db_session,
            news_source,
            first_name="NoPhoto",
            last_name="Player",
            has_photo=False,
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        assert result.featured == []
        compact_names = {p.display_name for p in result.compact}
        assert "OnlyOne Eligible" in compact_names
        assert "NoPhoto Player" in compact_names

    async def test_featured_caps_at_target_with_overflow_in_compact(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """If 6 players qualify, only FEATURED_TARGET land in featured."""
        eligible_count = FEATURED_TARGET + 2
        # Stagger mention counts so trending order is deterministic.
        for i in range(eligible_count):
            await _seed_player(
                db_session,
                news_source,
                first_name=f"P{i}",
                last_name="Eligible",
                mention_count=MIN_FEATURED_MENTIONS + (eligible_count - i),
            )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        assert len(result.featured) == FEATURED_TARGET
        # Featured rank pills should reflect actual mention rank (1..N).
        assert [p.rank for p in result.featured] == list(range(1, FEATURED_TARGET + 1))
        # Overflow lands in compact at their actual ranks.
        compact_ranks = sorted(p.rank for p in result.compact)
        assert compact_ranks[: 2] == [FEATURED_TARGET + 1, FEATURED_TARGET + 2]

    async def test_featured_skips_demoted_player_to_keep_target_count(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """A sparse player at rank 2 should be passed over and a later player promoted.

        Demonstrates the design where featured cards keep their actual mention
        rank pills (e.g. #1, #3, #4, #5 if #2 was demoted).
        """
        # Rank 1 and 3+ eligible, rank 2 missing photo.
        await _seed_player(
            db_session,
            news_source,
            first_name="Rank1",
            last_name="Eligible",
            mention_count=10,
        )
        await _seed_player(
            db_session,
            news_source,
            first_name="Rank2",
            last_name="NoPhoto",
            mention_count=8,
            has_photo=False,
        )
        await _seed_player(
            db_session,
            news_source,
            first_name="Rank3",
            last_name="Eligible",
            mention_count=7,
        )
        await db_session.commit()

        result = await get_expanded_trending_players(db_session)
        featured_ranks = sorted(p.rank for p in result.featured)
        assert featured_ranks == [1, 3]
        compact_ranks = sorted(p.rank for p in result.compact)
        assert compact_ranks == [2]

"""Integration tests for get_trending_players service function."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_content_mentions import ContentType, MentionSource, PlayerContentMention
from app.schemas.news_sources import NewsSource
from app.services.news_service import get_trending_players
from tests.integration.conftest import make_article, make_player


@pytest.mark.asyncio
class TestGetTrendingPlayers:
    """Tests for the get_trending_players service function."""

    async def test_empty_when_no_mentions(self, db_session: AsyncSession) -> None:
        """Returns empty list when no mention rows exist."""
        result = await get_trending_players(db_session)
        assert result == []

    async def test_returns_players_ordered_by_recency_weighted_score(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Players with more mentions rank higher by trending_score."""
        player_a = make_player("Cooper", "Flagg", school="Duke")
        player_b = make_player("Dylan", "Harper", school="Rutgers")
        db_session.add_all([player_a, player_b])
        await db_session.flush()

        # Player A: 3 articles published now, Player B: 1 article published now
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        articles = [
            make_article(news_source.id, f"art-{i}", hours_ago=0)  # type: ignore[arg-type]
            for i in range(4)
        ]
        for a in articles:
            db_session.add(a)
        await db_session.flush()

        mentions = [
            PlayerContentMention(
                content_type=ContentType.NEWS,
                content_id=articles[0].id,  # type: ignore[arg-type]
                player_id=player_a.id,  # type: ignore[arg-type]
                published_at=now,
                source=MentionSource.AI,
                created_at=now,
            ),
            PlayerContentMention(
                content_type=ContentType.NEWS,
                content_id=articles[1].id,  # type: ignore[arg-type]
                player_id=player_a.id,  # type: ignore[arg-type]
                published_at=now,
                source=MentionSource.AI,
                created_at=now,
            ),
            PlayerContentMention(
                content_type=ContentType.NEWS,
                content_id=articles[2].id,  # type: ignore[arg-type]
                player_id=player_a.id,  # type: ignore[arg-type]
                published_at=now,
                source=MentionSource.AI,
                created_at=now,
            ),
            PlayerContentMention(
                content_type=ContentType.NEWS,
                content_id=articles[3].id,  # type: ignore[arg-type]
                player_id=player_b.id,  # type: ignore[arg-type]
                published_at=now,
                source=MentionSource.AI,
                created_at=now,
            ),
        ]
        for m in mentions:
            db_session.add(m)
        await db_session.commit()

        result = await get_trending_players(db_session, days=7, limit=10)
        assert len(result) == 2
        assert result[0].player_id == player_a.id
        assert result[0].mention_count == 3
        assert result[0].trending_score > 0
        assert result[1].player_id == player_b.id
        assert result[1].mention_count == 1

    async def test_recency_weighting_affects_ranking(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """A player with 1 recent mention outranks a player with 1 old mention."""
        player_recent = make_player("Recent", "Player", school="Duke")
        player_old = make_player("Old", "Player", school="Rutgers")
        db_session.add_all([player_recent, player_old])
        await db_session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        old_pub = now - timedelta(days=6)

        # Article published ~0h ago vs ~6 days ago
        art_recent = make_article(
            news_source.id, "recent-art", hours_ago=0  # type: ignore[arg-type]
        )
        art_old = make_article(
            news_source.id, "old-art", hours_ago=6 * 24  # type: ignore[arg-type]
        )
        db_session.add_all([art_recent, art_old])
        await db_session.flush()

        db_session.add_all(
            [
                PlayerContentMention(
                    content_type=ContentType.NEWS,
                    content_id=art_recent.id,  # type: ignore[arg-type]
                    player_id=player_recent.id,  # type: ignore[arg-type]
                    published_at=now,
                    source=MentionSource.AI,
                    created_at=now,
                ),
                PlayerContentMention(
                    content_type=ContentType.NEWS,
                    content_id=art_old.id,  # type: ignore[arg-type]
                    player_id=player_old.id,  # type: ignore[arg-type]
                    published_at=old_pub,
                    source=MentionSource.AI,
                    created_at=now - timedelta(days=6),
                ),
            ]
        )
        await db_session.commit()

        result = await get_trending_players(db_session, days=7, limit=10)
        assert len(result) == 2
        # Recent player should rank first due to higher recency weight
        assert result[0].player_id == player_recent.id
        assert result[0].trending_score > result[1].trending_score
        assert result[1].player_id == player_old.id

    async def test_daily_counts_populated(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Daily counts list has correct length and non-zero values on the right days."""
        player = make_player("Daily", "Count", school="UNC")
        db_session.add(player)
        await db_session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        pub_today = now - timedelta(hours=1)
        pub_2d = now - timedelta(hours=48)

        # Articles published today and 2 days ago
        art_today = make_article(
            news_source.id, "today-art", hours_ago=1  # type: ignore[arg-type]
        )
        art_2d = make_article(
            news_source.id, "2d-art", hours_ago=48  # type: ignore[arg-type]
        )
        db_session.add_all([art_today, art_2d])
        await db_session.flush()

        db_session.add_all(
            [
                PlayerContentMention(
                    content_type=ContentType.NEWS,
                    content_id=art_today.id,  # type: ignore[arg-type]
                    player_id=player.id,  # type: ignore[arg-type]
                    published_at=pub_today,
                    source=MentionSource.AI,
                    created_at=now,
                ),
                PlayerContentMention(
                    content_type=ContentType.NEWS,
                    content_id=art_2d.id,  # type: ignore[arg-type]
                    player_id=player.id,  # type: ignore[arg-type]
                    published_at=pub_2d,
                    source=MentionSource.AI,
                    created_at=now - timedelta(days=2),
                ),
            ]
        )
        await db_session.commit()

        result = await get_trending_players(db_session, days=7, limit=10)
        assert len(result) == 1
        tp = result[0]
        # daily_counts should be a list of 7 ints (oldest-first)
        assert len(tp.daily_counts) == 7
        assert sum(tp.daily_counts) == 2
        # Last element (today) should be >= 1
        assert tp.daily_counts[-1] >= 1

    async def test_excludes_mentions_outside_time_window(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Mentions older than the time window are excluded."""
        player = make_player("Old", "Mention")
        db_session.add(player)
        await db_session.flush()

        # Article published 10 days ago
        article = make_article(
            news_source.id, "old-art", hours_ago=10 * 24  # type: ignore[arg-type]
        )
        db_session.add(article)
        await db_session.flush()

        old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
        mention = PlayerContentMention(
            content_type=ContentType.NEWS,
            content_id=article.id,  # type: ignore[arg-type]
            player_id=player.id,  # type: ignore[arg-type]
            published_at=old_time,
            source=MentionSource.AI,
            created_at=old_time,
        )
        db_session.add(mention)
        await db_session.commit()

        # Default 7-day window should exclude it
        result = await get_trending_players(db_session, days=7)
        assert result == []

        # Wider window should include it
        result = await get_trending_players(db_session, days=30)
        assert len(result) == 1
        assert result[0].player_id == player.id

    async def test_includes_player_metadata(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Trending results include display_name, slug, school, and new fields."""
        player = make_player("Ace", "Bailey", school="Rutgers")
        db_session.add(player)
        await db_session.flush()

        article = make_article(
            news_source.id, "meta-art", hours_ago=0  # type: ignore[arg-type]
        )
        db_session.add(article)
        await db_session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        mention = PlayerContentMention(
            content_type=ContentType.NEWS,
            content_id=article.id,  # type: ignore[arg-type]
            player_id=player.id,  # type: ignore[arg-type]
            published_at=now,
            source=MentionSource.AI,
            created_at=now,
        )
        db_session.add(mention)
        await db_session.commit()

        result = await get_trending_players(db_session)
        assert len(result) == 1
        tp = result[0]
        assert tp.display_name == "Ace Bailey"
        assert tp.slug is not None
        assert "ace-bailey" in tp.slug
        assert tp.school == "Rutgers"
        assert tp.trending_score > 0
        assert isinstance(tp.daily_counts, list)
        assert len(tp.daily_counts) == 7
        assert tp.latest_mention_at is not None

    async def test_respects_limit(
        self, db_session: AsyncSession, news_source: NewsSource
    ) -> None:
        """Limit parameter caps the number of returned players."""
        players = [make_player(f"Player{i}", "Test") for i in range(5)]
        db_session.add_all(players)
        await db_session.flush()

        articles = [
            make_article(news_source.id, f"lim-art-{i}", hours_ago=0)  # type: ignore[arg-type]
            for i in range(5)
        ]
        for a in articles:
            db_session.add(a)
        await db_session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for i, player in enumerate(players):
            mention = PlayerContentMention(
                content_type=ContentType.NEWS,
                content_id=articles[i].id,  # type: ignore[arg-type]
                player_id=player.id,  # type: ignore[arg-type]
                published_at=now,
                source=MentionSource.AI,
                created_at=now,
            )
            db_session.add(mention)
        await db_session.commit()

        result = await get_trending_players(db_session, limit=3)
        assert len(result) == 3

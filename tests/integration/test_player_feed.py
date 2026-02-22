"""Integration tests for get_player_news_feed service function."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_content_mentions import ContentType, MentionSource, PlayerContentMention
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services.news_service import get_player_news_feed
from tests.integration.conftest import make_article


@pytest_asyncio.fixture()
async def target_player(db_session: AsyncSession) -> PlayerMaster:
    """Create the player whose feed we're testing."""
    player = PlayerMaster(
        first_name="Cooper",
        last_name="Flagg",
        display_name="Cooper Flagg",
        draft_year=2025,
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()
    return player


@pytest.mark.asyncio
class TestGetPlayerNewsFeed:
    """Tests for the get_player_news_feed service function."""

    async def test_returns_articles_via_mention(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """Articles linked via the mention junction table are returned."""
        article = make_article(news_source.id, "mention-art")  # type: ignore[arg-type]
        db_session.add(article)
        await db_session.flush()

        mention = PlayerContentMention(
            content_type=ContentType.NEWS,
            content_id=article.id,  # type: ignore[arg-type]
            player_id=target_player.id,  # type: ignore[arg-type]
            source=MentionSource.AI,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db_session.add(mention)
        await db_session.commit()

        result = await get_player_news_feed(
            db_session, player_id=target_player.id  # type: ignore[arg-type]
        )
        assert len(result.items) >= 1
        titles = [item.title for item in result.items]
        assert "Article mention-art" in titles

    async def test_returns_articles_via_direct_player_id(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """Articles with NewsItem.player_id set directly are returned."""
        article = make_article(
            news_source.id,  # type: ignore[arg-type]
            "direct-art",
            player_id=target_player.id,  # type: ignore[arg-type]
        )
        db_session.add(article)
        await db_session.commit()

        result = await get_player_news_feed(
            db_session, player_id=target_player.id  # type: ignore[arg-type]
        )
        titles = [item.title for item in result.items]
        assert "Article direct-art" in titles

    async def test_no_duplicates_between_mention_and_direct(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """An article linked both ways should appear only once."""
        article = make_article(
            news_source.id,  # type: ignore[arg-type]
            "both-art",
            player_id=target_player.id,  # type: ignore[arg-type]
        )
        db_session.add(article)
        await db_session.flush()

        mention = PlayerContentMention(
            content_type=ContentType.NEWS,
            content_id=article.id,  # type: ignore[arg-type]
            player_id=target_player.id,  # type: ignore[arg-type]
            source=MentionSource.AI,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db_session.add(mention)
        await db_session.commit()

        result = await get_player_news_feed(
            db_session, player_id=target_player.id  # type: ignore[arg-type]
        )
        article_ids = [item.id for item in result.items if item.title == "Article both-art"]
        assert len(article_ids) == 1

    async def test_backfills_when_insufficient_player_articles(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """When player-specific articles < min_items, general articles are added."""
        # Create 1 player-specific article
        player_art = make_article(
            news_source.id,  # type: ignore[arg-type]
            "player-only",
            player_id=target_player.id,  # type: ignore[arg-type]
        )
        db_session.add(player_art)

        # Create several general articles (no player association)
        for i in range(5):
            general = make_article(
                news_source.id,  # type: ignore[arg-type]
                f"general-{i}",
                hours_ago=2 + i,
            )
            db_session.add(general)
        await db_session.commit()

        result = await get_player_news_feed(
            db_session,
            player_id=target_player.id,  # type: ignore[arg-type]
            min_items=5,
        )
        # Should have the 1 player-specific + 4 backfilled = 5 total
        assert len(result.items) >= 5

    async def test_is_player_specific_flag(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """Player-specific items are flagged; backfilled items are not."""
        # Player article
        player_art = make_article(
            news_source.id,  # type: ignore[arg-type]
            "flagged-art",
            player_id=target_player.id,  # type: ignore[arg-type]
        )
        db_session.add(player_art)

        # General article for backfill
        general_art = make_article(
            news_source.id,  # type: ignore[arg-type]
            "general-art",
            hours_ago=5,
        )
        db_session.add(general_art)
        await db_session.commit()

        result = await get_player_news_feed(
            db_session,
            player_id=target_player.id,  # type: ignore[arg-type]
            min_items=3,
        )
        player_items = [i for i in result.items if i.is_player_specific]
        general_items = [i for i in result.items if not i.is_player_specific]
        assert len(player_items) >= 1
        assert any(i.title == "Article flagged-art" for i in player_items)
        assert any(i.title == "Article general-art" for i in general_items)

    async def test_empty_feed_for_player_with_no_articles(
        self,
        db_session: AsyncSession,
        target_player: PlayerMaster,
    ) -> None:
        """Player with no mentions or direct articles returns empty feed."""
        result = await get_player_news_feed(
            db_session,
            player_id=target_player.id,  # type: ignore[arg-type]
            min_items=0,
        )
        assert result.items == []
        assert result.total == 0

    async def test_total_count_reflects_player_specific_only(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """The total count should reflect player-specific articles, not backfill."""
        # Create 2 player-specific articles
        for i in range(2):
            art = make_article(
                news_source.id,  # type: ignore[arg-type]
                f"count-art-{i}",
                player_id=target_player.id,  # type: ignore[arg-type]
                hours_ago=i + 1,
            )
            db_session.add(art)

        # Create general articles
        for i in range(3):
            general = make_article(
                news_source.id,  # type: ignore[arg-type]
                f"count-general-{i}",
                hours_ago=10 + i,
            )
            db_session.add(general)
        await db_session.commit()

        result = await get_player_news_feed(
            db_session,
            player_id=target_player.id,  # type: ignore[arg-type]
            min_items=5,
        )
        # Total should be 2 (player-specific only), even though items list has backfill
        assert result.total == 2
        assert len(result.items) >= 5


@pytest.mark.asyncio
class TestPlayerFeedAPI:
    """Tests for the /api/news?player_id= endpoint."""

    async def test_api_returns_player_feed(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        news_source: NewsSource,
        target_player: PlayerMaster,
    ) -> None:
        """GET /api/news?player_id=X delegates to player-specific feed."""
        article = make_article(
            news_source.id,  # type: ignore[arg-type]
            "api-art",
            player_id=target_player.id,  # type: ignore[arg-type]
        )
        db_session.add(article)
        await db_session.commit()

        response = await app_client.get(f"/api/news?player_id={target_player.id}")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) >= 1
        titles = [item["title"] for item in data["items"]]
        assert "Article api-art" in titles

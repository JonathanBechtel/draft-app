"""Integration tests for _persist_player_mentions in news_ingestion_service."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_content_mentions import ContentType, MentionSource, PlayerContentMention
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services.news_ingestion_service import _persist_player_mentions
from tests.integration.conftest import make_article, make_player


@pytest.mark.asyncio
class TestPersistPlayerMentions:
    """Tests for the _persist_player_mentions ingestion phase."""

    async def test_creates_mention_rows_for_known_players(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Known player names are resolved and mention rows are inserted."""
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()

        article = make_article(news_source.id, "ext-1")  # type: ignore[arg-type]
        db_session.add(article)
        await db_session.commit()

        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={"ext-1": ["Cooper Flagg"]},
        )
        assert inserted == 1

        # Verify the row in the database
        stmt = select(PlayerContentMention).where(  # type: ignore[call-overload]
            PlayerContentMention.content_id == article.id,  # type: ignore[arg-type]
            PlayerContentMention.content_type == ContentType.NEWS,  # type: ignore[arg-type]
        )
        rows = (await db_session.execute(stmt)).scalars().all()
        assert len(rows) == 1
        assert rows[0].player_id == player.id
        assert rows[0].source == MentionSource.AI

    async def test_creates_stub_for_unknown_player(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Unknown player names create stub records and mention rows."""
        article = make_article(news_source.id, "ext-2")  # type: ignore[arg-type]
        db_session.add(article)
        await db_session.commit()

        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={"ext-2": ["Brand New Prospect"]},
        )
        assert inserted == 1

        # Verify stub player was created
        stmt = select(PlayerMaster).where(
            PlayerMaster.display_name == "Brand New Prospect"
        )
        stub = (await db_session.execute(stmt)).scalar_one()
        assert stub.is_stub is True

    async def test_multiple_players_per_article(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Multiple player names for one article create separate mention rows."""
        player_a = make_player("Cooper", "Flagg")
        player_b = make_player("Ace", "Bailey")
        db_session.add_all([player_a, player_b])
        await db_session.flush()

        article = make_article(news_source.id, "ext-3")  # type: ignore[arg-type]
        db_session.add(article)
        await db_session.commit()

        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={"ext-3": ["Cooper Flagg", "Ace Bailey"]},
        )
        assert inserted == 2

    async def test_deduplicates_same_player_in_article(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Duplicate player name in the same article produces only one mention."""
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()

        article = make_article(news_source.id, "ext-4")  # type: ignore[arg-type]
        db_session.add(article)
        await db_session.commit()

        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={"ext-4": ["Cooper Flagg", "Cooper Flagg"]},
        )
        assert inserted == 1

    async def test_skips_unknown_external_ids(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """External IDs not in the database are silently skipped."""
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()
        await db_session.commit()

        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={"nonexistent-ext-id": ["Cooper Flagg"]},
        )
        assert inserted == 0

    async def test_empty_mention_map_returns_zero(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Empty mention_map returns zero without any DB operations."""
        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={},
        )
        assert inserted == 0

    async def test_conflict_handling_on_duplicate_insert(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Re-running with same data should not raise and returns 0 new inserts."""
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()

        article = make_article(news_source.id, "ext-dup")  # type: ignore[arg-type]
        db_session.add(article)
        await db_session.commit()

        mention_map = {"ext-dup": ["Cooper Flagg"]}

        first = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map=mention_map,
        )
        assert first == 1

        # Second call with same data should skip via ON CONFLICT DO NOTHING
        second = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map=mention_map,
        )
        assert second == 0

    async def test_multiple_articles_with_mentions(
        self,
        db_session: AsyncSession,
        news_source: NewsSource,
    ) -> None:
        """Mention map spanning multiple articles creates correct rows."""
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()

        art1 = make_article(news_source.id, "multi-1")  # type: ignore[arg-type]
        art2 = make_article(news_source.id, "multi-2", hours_ago=2)  # type: ignore[arg-type]
        db_session.add_all([art1, art2])
        await db_session.commit()

        inserted = await _persist_player_mentions(
            db_session,
            source_id=news_source.id,  # type: ignore[arg-type]
            mention_map={
                "multi-1": ["Cooper Flagg"],
                "multi-2": ["Cooper Flagg"],
            },
        )
        assert inserted == 2

        # Verify each article has its own mention row
        stmt = select(PlayerContentMention).where(  # type: ignore[call-overload]
            PlayerContentMention.player_id == player.id  # type: ignore[arg-type]
        )
        rows = (await db_session.execute(stmt)).scalars().all()
        article_ids = {r.content_id for r in rows}
        assert art1.id in article_ids
        assert art2.id in article_ids

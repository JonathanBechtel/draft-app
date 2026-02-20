"""Integration tests for the podcast feed API endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.podcast_episodes import PodcastEpisode, PodcastEpisodeTag
from app.schemas.podcast_shows import PodcastShow
from app.schemas.player_content_mentions import ContentType, MentionSource, PlayerContentMention
from app.schemas.players_master import PlayerMaster
from tests.integration.auth_helpers import create_auth_user, login_staff
from tests.integration.conftest import make_player, make_podcast_episode, make_podcast_show

ADMIN_EMAIL = "podcast-admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def admin_client(app_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    """Return an authenticated admin client for staff-only podcast endpoints."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    response = await login_staff(
        app_client,
        email=ADMIN_EMAIL,
        password=ADMIN_PASSWORD,
    )
    assert response.status_code in {302, 303}
    return app_client


@pytest_asyncio.fixture
async def sample_show(db_session: AsyncSession) -> PodcastShow:
    """Create a sample podcast show for testing."""
    show = make_podcast_show()
    db_session.add(show)
    await db_session.commit()
    await db_session.refresh(show)
    return show


@pytest_asyncio.fixture
async def sample_episode(
    db_session: AsyncSession, sample_show: PodcastShow
) -> PodcastEpisode:
    """Create a sample podcast episode for testing."""
    episode = make_podcast_episode(
        show_id=sample_show.id,  # type: ignore[arg-type]
        external_id="ep-1",
    )
    db_session.add(episode)
    await db_session.commit()
    await db_session.refresh(episode)
    return episode


@pytest.mark.asyncio
class TestListPodcasts:
    """Tests for GET /api/podcasts endpoint."""

    async def test_list_podcasts_empty(self, app_client: AsyncClient):
        """GET /api/podcasts returns empty list when no episodes exist."""
        response = await app_client.get("/api/podcasts")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_podcasts_returns_episodes(
        self,
        app_client: AsyncClient,
        sample_episode: PodcastEpisode,
    ):
        """GET /api/podcasts returns episodes with correct format."""
        response = await app_client.get("/api/podcasts")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 1
        assert len(data["items"]) == 1

        item = data["items"][0]
        assert item["title"] == "Episode ep-1"
        assert item["show_name"] == "Test Draft Pod"
        assert item["tag"] == "Draft Analysis"
        assert "audio_url" in item
        assert "duration" in item
        assert "listen_on_text" in item

    async def test_list_podcasts_with_player_id(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        sample_show: PodcastShow,
    ):
        """GET /api/podcasts?player_id=X returns mention-linked episodes."""
        # Create a player
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()

        # Create an episode
        episode = make_podcast_episode(
            show_id=sample_show.id,  # type: ignore[arg-type]
            external_id="ep-player-1",
        )
        db_session.add(episode)
        await db_session.flush()

        # Create a mention linking the player to the episode
        mention = PlayerContentMention(
            content_type=ContentType.PODCAST,
            content_id=episode.id,  # type: ignore[arg-type]
            player_id=player.id,  # type: ignore[arg-type]
            published_at=episode.published_at,
            source=MentionSource.AI,
        )
        db_session.add(mention)
        await db_session.commit()

        response = await app_client.get(f"/api/podcasts?player_id={player.id}")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 1
        assert data["items"][0]["title"] == "Episode ep-player-1"
        assert data["items"][0]["is_player_specific"] is True


@pytest.mark.asyncio
class TestPodcastSources:
    """Tests for podcast show management endpoints."""

    async def test_create_show_requires_auth(self, app_client: AsyncClient):
        """POST /api/podcasts/sources without auth returns 401."""
        response = await app_client.post(
            "/api/podcasts/sources",
            json={
                "name": "New Pod",
                "display_name": "New Pod Display",
                "feed_url": "https://newpod.com/feed",
            },
        )
        assert response.status_code == 401

    async def test_list_sources_requires_auth(self, app_client: AsyncClient):
        """GET /api/podcasts/sources without auth returns 401."""
        response = await app_client.get("/api/podcasts/sources")
        assert response.status_code == 401

    async def test_create_show(self, admin_client: AsyncClient):
        """POST /api/podcasts/sources creates a new show."""
        response = await admin_client.post(
            "/api/podcasts/sources",
            json={
                "name": "New Pod",
                "display_name": "New Pod Display",
                "feed_url": "https://newpod.com/feed",
                "fetch_interval_minutes": 60,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Pod"
        assert data["display_name"] == "New Pod Display"
        assert data["feed_url"] == "https://newpod.com/feed"
        assert data["is_active"] is True
        assert data["is_draft_focused"] is True
        assert "id" in data

    async def test_create_show_rejects_duplicate_feed_url(
        self,
        admin_client: AsyncClient,
        sample_show: PodcastShow,
    ):
        """POST /api/podcasts/sources rejects duplicate feed URLs."""
        response = await admin_client.post(
            "/api/podcasts/sources",
            json={
                "name": "Dupe",
                "display_name": "Dupe",
                "feed_url": sample_show.feed_url,
            },
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]


@pytest.mark.asyncio
class TestTriggerPodcastIngestion:
    """Tests for POST /api/podcasts/ingest endpoint."""

    async def test_trigger_ingestion_requires_auth(self, app_client: AsyncClient):
        """POST /api/podcasts/ingest without auth returns 401."""
        response = await app_client.post("/api/podcasts/ingest")
        assert response.status_code == 401

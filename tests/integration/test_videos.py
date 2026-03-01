"""Integration tests for film-room video APIs and UI surfaces."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_content_mentions import ContentType, MentionSource, PlayerContentMention
from app.schemas.players_master import PlayerMaster
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo
from tests.integration.auth_helpers import create_auth_user, login_staff
from tests.integration.conftest import make_player, make_youtube_channel, make_youtube_video

ADMIN_EMAIL = "videos-admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def admin_client(app_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    """Return an authenticated admin client for staff-only video endpoints."""
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
async def sample_channel(db_session: AsyncSession) -> YouTubeChannel:
    """Create a sample YouTube channel."""
    channel = make_youtube_channel()
    db_session.add(channel)
    await db_session.commit()
    await db_session.refresh(channel)
    return channel


@pytest_asyncio.fixture
async def sample_video(
    db_session: AsyncSession,
    sample_channel: YouTubeChannel,
) -> YouTubeVideo:
    """Create a sample YouTube video."""
    video = make_youtube_video(
        channel_id=sample_channel.id,  # type: ignore[arg-type]
        external_id="dQw4w9WgXcQ",
    )
    db_session.add(video)
    await db_session.commit()
    await db_session.refresh(video)
    return video


@pytest.mark.asyncio
class TestListVideos:
    """Tests for GET /api/videos endpoint."""

    async def test_list_videos_empty(self, app_client: AsyncClient) -> None:
        """GET /api/videos returns empty payload when no videos exist."""
        response = await app_client.get("/api/videos")
        assert response.status_code == 200
        payload = response.json()
        assert payload["items"] == []
        assert payload["total"] == 0

    async def test_list_videos_returns_items(
        self,
        app_client: AsyncClient,
        sample_video: YouTubeVideo,
    ) -> None:
        """GET /api/videos returns expected fields."""
        response = await app_client.get("/api/videos")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        item = payload["items"][0]
        assert item["title"] == sample_video.title
        assert item["youtube_embed_id"] == sample_video.external_id
        assert item["tag"] == "Scouting Report"

    async def test_list_videos_with_player_filter(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        sample_video: YouTubeVideo,
    ) -> None:
        """Player filter returns mention-linked video rows."""
        player = make_player("Cooper", "Flagg")
        db_session.add(player)
        await db_session.flush()

        mention = PlayerContentMention(
            content_type=ContentType.VIDEO,
            content_id=sample_video.id,  # type: ignore[arg-type]
            player_id=player.id,  # type: ignore[arg-type]
            published_at=sample_video.published_at,
            source=MentionSource.AI,
        )
        db_session.add(mention)
        await db_session.commit()

        response = await app_client.get(f"/api/videos?player_id={player.id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["is_player_specific"] is True


@pytest.mark.asyncio
class TestVideoSources:
    """Tests for YouTube channel management endpoints."""

    async def test_list_sources_requires_auth(self, app_client: AsyncClient) -> None:
        """GET /api/videos/sources without auth returns 401."""
        response = await app_client.get("/api/videos/sources")
        assert response.status_code == 401

    async def test_create_source_requires_auth(self, app_client: AsyncClient) -> None:
        """POST /api/videos/sources without auth returns 401."""
        response = await app_client.post(
            "/api/videos/sources",
            json={
                "name": "Draft Film",
                "display_name": "Draft Film",
                "channel_id": "UC123",
            },
        )
        assert response.status_code == 401

    async def test_create_source(
        self,
        admin_client: AsyncClient,
    ) -> None:
        """POST /api/videos/sources creates a new channel."""
        response = await admin_client.post(
            "/api/videos/sources",
            json={
                "name": "Draft Film",
                "display_name": "Draft Film",
                "channel_id": "UC1234",
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["name"] == "Draft Film"
        assert payload["channel_id"] == "UC1234"

    async def test_create_source_duplicate_rejected(
        self,
        admin_client: AsyncClient,
        sample_channel: YouTubeChannel,
    ) -> None:
        """Duplicate channel_id is rejected with 409."""
        response = await admin_client.post(
            "/api/videos/sources",
            json={
                "name": "Dupe",
                "display_name": "Dupe",
                "channel_id": sample_channel.channel_id,
            },
        )
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_video_ingest_requires_auth(app_client: AsyncClient) -> None:
    """POST /api/videos/ingest without auth returns 401."""
    response = await app_client.post("/api/videos/ingest")
    assert response.status_code == 401


@pytest.mark.asyncio
class TestFilmRoomPages:
    """Tests for film-room UI surfaces."""

    async def test_film_room_page_renders(self, app_client: AsyncClient) -> None:
        """GET /film-room returns HTML page."""
        response = await app_client.get("/film-room")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Film Room" in response.text

    async def test_homepage_includes_film_room_section_when_videos_exist(
        self,
        app_client: AsyncClient,
        sample_video: YouTubeVideo,
    ) -> None:
        """Homepage renders Film Room section when videos exist."""
        _ = sample_video
        response = await app_client.get("/")
        assert response.status_code == 200
        assert "filmRoomHomeSection" in response.text

    async def test_player_page_shows_no_video_placeholder(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """Player page renders no-videos message when no linked videos."""
        player = make_player("No", "Video")
        db_session.add(player)
        await db_session.commit()
        await db_session.refresh(player)

        response = await app_client.get(f"/players/{player.slug}")
        assert response.status_code == 200
        assert "film-no-videos" in response.text

    async def test_player_page_renders_player_video_cards(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        sample_video: YouTubeVideo,
    ) -> None:
        """Player page includes film-study cards for mention-linked videos."""
        player = PlayerMaster(
            first_name="Ace",
            last_name="Bailey",
            display_name="Ace Bailey",
            draft_year=2025,
            is_stub=False,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            updated_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db_session.add(player)
        await db_session.flush()

        mention = PlayerContentMention(
            content_type=ContentType.VIDEO,
            content_id=sample_video.id,  # type: ignore[arg-type]
            player_id=player.id,  # type: ignore[arg-type]
            published_at=sample_video.published_at,
            source=MentionSource.AI,
        )
        db_session.add(mention)
        await db_session.commit()
        await db_session.refresh(player)

        response = await app_client.get(f"/players/{player.slug}")
        assert response.status_code == 200
        assert sample_video.title in response.text

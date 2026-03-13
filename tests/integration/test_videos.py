"""Integration tests for film-room video APIs and UI surfaces."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from bs4 import BeautifulSoup, Tag
from httpx import AsyncClient
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_content_mentions import ContentType, MentionSource, PlayerContentMention
from app.schemas.players_master import PlayerMaster
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo
from tests.integration.auth_helpers import create_auth_user, login_staff
from tests.integration.conftest import make_player, make_youtube_channel, make_youtube_video

ADMIN_EMAIL = "videos-admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


def _channel_sidebar_links(soup: BeautifulSoup) -> list[Tag]:
    """Return the Film Room channel filter links from the sidebar."""
    cards = soup.select(".film-room-sidebar .sidebar-card")
    assert cards
    return cards[0].select("a.film-room-sidebar__item")


def _find_sidebar_link(links: list[Tag], label: str) -> Tag:
    """Find a channel sidebar link by its visible text label."""
    for link in links:
        spans = link.find_all("span")
        visible_label = spans[-1].get_text(strip=True) if spans else link.get_text(strip=True)
        if visible_label == label:
            return link
    raise AssertionError(f"Could not find sidebar link with label {label!r}")


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
async def test_manual_add_video_returns_400_for_invalid_input(
    admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual add returns 400 when validation fails for user-correctable input."""

    async def _fake_add_video_by_url(**_: object) -> int:
        raise ValueError("Invalid YouTube URL")

    monkeypatch.setattr("app.routes.videos.add_video_by_url", _fake_add_video_by_url)

    response = await admin_client.post(
        "/api/videos/add",
        json={"youtube_url": "not-a-youtube-url", "player_ids": []},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid YouTube URL"}


@pytest.mark.asyncio
async def test_manual_add_video_returns_503_when_api_key_missing(
    admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual add returns 503 when the server is missing required YouTube config."""

    async def _fake_add_video_by_url(**_: object) -> int:
        raise ValueError("YOUTUBE_API_KEY is not configured")

    monkeypatch.setattr("app.routes.videos.add_video_by_url", _fake_add_video_by_url)

    response = await admin_client.post(
        "/api/videos/add",
        json={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "YOUTUBE_API_KEY is not configured"}


@pytest.mark.asyncio
class TestFilmRoomPages:
    """Tests for film-room UI surfaces."""

    async def test_film_room_page_renders(self, app_client: AsyncClient) -> None:
        """GET /film-room returns HTML page."""
        response = await app_client.get("/film-room")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Film Room" in response.text

    async def test_film_room_page_renders_all_channels_item_as_default_reset(
        self,
        app_client: AsyncClient,
        sample_video: YouTubeVideo,
    ) -> None:
        """Film Room renders an active All Channels item when no channel is selected."""
        _ = sample_video
        response = await app_client.get("/film-room")

        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        links = _channel_sidebar_links(soup)
        all_channels = _find_sidebar_link(links, "All Channels")

        assert links[0] == all_channels
        assert "film-room-sidebar__item--active" in (all_channels.get("class") or [])
        assert urlsplit(all_channels["href"]).path == "/film-room"
        assert parse_qs(urlsplit(all_channels["href"]).query) == {}

    async def test_film_room_page_selected_channel_keeps_reset_link(
        self,
        app_client: AsyncClient,
        sample_channel: YouTubeChannel,
        sample_video: YouTubeVideo,
    ) -> None:
        """Selected channel is highlighted and All Channels clears only that filter."""
        _ = sample_video
        response = await app_client.get(f"/film-room?channel={sample_channel.id}")

        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        links = _channel_sidebar_links(soup)
        all_channels = _find_sidebar_link(links, "All Channels")
        selected_channel = _find_sidebar_link(links, sample_channel.display_name)

        assert "film-room-sidebar__item--active" not in (all_channels.get("class") or [])
        assert parse_qs(urlsplit(all_channels["href"]).query) == {}
        assert "film-room-sidebar__item--active" in (selected_channel.get("class") or [])
        assert parse_qs(urlsplit(selected_channel["href"]).query) == {
            "channel": [str(sample_channel.id)]
        }

    async def test_film_room_channel_links_preserve_other_active_filters(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        sample_channel: YouTubeChannel,
        sample_video: YouTubeVideo,
    ) -> None:
        """Channel links preserve tag, player, and search while changing only channel."""
        second_channel = make_youtube_channel(
            name="Second Film Hub",
            channel_id="UC_second_film_hub",
        )
        db_session.add(second_channel)
        await db_session.flush()

        second_video = make_youtube_video(
            channel_id=second_channel.id,  # type: ignore[arg-type]
            external_id="altVideo123",
        )
        db_session.add(second_video)

        player = make_player("VJ", "Edgecombe")
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

        response = await app_client.get(
            f"/film-room?tag=Scouting+Report&channel={sample_channel.id}"
            f"&player={player.id}&search=Video"
        )

        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        links = _channel_sidebar_links(soup)
        all_channels = _find_sidebar_link(links, "All Channels")
        other_channel = _find_sidebar_link(links, second_channel.display_name)

        assert parse_qs(urlsplit(all_channels["href"]).query) == {
            "tag": ["Scouting Report"],
            "player": [str(player.id)],
            "search": ["Video"],
        }
        assert parse_qs(urlsplit(other_channel["href"]).query) == {
            "tag": ["Scouting Report"],
            "channel": [str(second_channel.id)],
            "player": [str(player.id)],
            "search": ["Video"],
        }

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
        """Player page renders film playlist UI for mention-linked videos."""
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
        assert "film-playlist" in response.text
        assert "film-thumb" in response.text
        assert sample_video.title in response.text


@pytest.mark.asyncio
async def test_update_youtube_video_rejects_invalid_manual_player_ids_atomically(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    sample_video: YouTubeVideo,
) -> None:
    """Invalid manual player IDs roll back the video edit instead of partially saving."""
    original_title = sample_video.title
    original_summary = sample_video.summary
    original_tag = sample_video.tag

    response = await admin_client.post(
        f"/admin/youtube-videos/{sample_video.id}",
        data={
            "title": "Updated title that should roll back",
            "summary": "Updated summary that should roll back",
            "tag": "MONTAGE",
            "player_ids": ["999999"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Invalid manual player ID(s): 999999" in response.text

    refreshed_video = await db_session.get(YouTubeVideo, sample_video.id)
    assert refreshed_video is not None
    assert refreshed_video.title == original_title
    assert refreshed_video.summary == original_summary
    assert refreshed_video.tag == original_tag

    mention_rows = (
        (
            await db_session.execute(
                select(PlayerContentMention).where(
                    PlayerContentMention.content_type == ContentType.VIDEO,  # type: ignore[arg-type]
                    PlayerContentMention.content_id == sample_video.id,  # type: ignore[arg-type]
                    PlayerContentMention.source == MentionSource.MANUAL,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert mention_rows == []

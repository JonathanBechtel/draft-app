"""Integration tests for admin PlayerMaster CRUD."""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "players-admin@example.com"
ADMIN_PASSWORD = "admin-password-123"
WORKER_EMAIL = "players-worker@example.com"
WORKER_PASSWORD = "worker-password-456"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin auth user for players CRUD tests."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )


@pytest_asyncio.fixture
async def worker_user_id(db_session: AsyncSession) -> int:
    """Create a worker auth user for players CRUD tests."""
    return await create_auth_user(
        db_session,
        email=WORKER_EMAIL,
        role="worker",
        password=WORKER_PASSWORD,
    )


@pytest_asyncio.fixture
async def sample_player_id(db_session: AsyncSession) -> int:
    """Create a sample player for edit/delete tests."""
    player = PlayerMaster(
        display_name="Test Player",
        first_name="Test",
        last_name="Player",
        school="Test University",
        draft_year=2024,
        draft_round=1,
        draft_pick=5,
    )
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)
    assert player.id is not None
    return player.id


@pytest_asyncio.fixture
async def sample_news_source_id(db_session: AsyncSession) -> int:
    """Create a sample news source for linking news items to players."""
    source = NewsSource(
        name="player-test-source",
        display_name="Player Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/player-test-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    assert source.id is not None
    return source.id


@pytest.mark.asyncio
class TestPlayersAccess:
    """Tests for access control on players pages."""

    async def test_list_requires_login(self, app_client: AsyncClient):
        """GET /admin/players redirects when not authenticated."""
        response = await app_client.get("/admin/players", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")

    async def test_list_requires_admin_role(
        self,
        app_client: AsyncClient,
        worker_user_id: int,
    ):
        """Workers are redirected away from players."""
        _ = worker_user_id
        await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)

        response = await app_client.get("/admin/players", follow_redirects=False)
        assert response.status_code in {302, 303}
        # Should redirect to dashboard, not show players
        assert response.headers.get("location") == "/admin"

    async def test_list_works_for_admin(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Admins can access the players list."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players")
        assert response.status_code == 200
        assert "Players" in response.text


@pytest.mark.asyncio
class TestPlayersList:
    """Tests for the players list page."""

    async def test_empty_list_shows_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Empty list shows a helpful message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players")
        assert response.status_code == 200
        assert "No Players" in response.text or "Add Player" in response.text

    async def test_list_shows_players(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """List shows existing players."""
        _ = admin_user_id
        _ = sample_player_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players")
        assert response.status_code == 200
        assert "Test Player" in response.text

    async def test_list_shows_success_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Success query param shows appropriate message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players?success=created")
        assert response.status_code == 200
        assert "created" in response.text.lower()

    async def test_search_filter_works(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Search filter finds players by name."""
        _ = admin_user_id
        _ = sample_player_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players?q=Test")
        assert response.status_code == 200
        assert "Test Player" in response.text

    async def test_draft_year_filter_works(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Draft year filter shows matching players."""
        _ = admin_user_id
        _ = sample_player_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players?draft_year=2024")
        assert response.status_code == 200
        assert "Test Player" in response.text

        # Non-matching year should not show player
        response = await app_client.get("/admin/players?draft_year=2020")
        assert response.status_code == 200
        assert "Test Player" not in response.text


@pytest.mark.asyncio
class TestPlayersCreate:
    """Tests for creating players."""

    async def test_new_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/players/new shows the form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players/new")
        assert response.status_code == 200
        assert 'name="display_name"' in response.text
        assert 'name="first_name"' in response.text
        assert 'name="last_name"' in response.text

    async def test_create_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """POST /admin/players creates a new player."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/players",
            data={
                "display_name": "New Player",
                "first_name": "New",
                "last_name": "Player",
                "school": "New University",
                "draft_year": "2025",
                "draft_round": "1",
                "draft_pick": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=created" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text("SELECT display_name FROM players_master WHERE display_name = 'New Player'")
        )
        assert result.scalar_one() == "New Player"

    async def test_create_missing_display_name_error(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Creating without display_name shows error."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/players",
            data={
                "display_name": "",
                "first_name": "Test",
                "last_name": "Player",
            },
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()

    async def test_create_with_all_fields(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """POST /admin/players creates a player with all fields."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/players",
            data={
                "display_name": "Complete Player",
                "first_name": "Complete",
                "last_name": "Player",
                "prefix": "Mr.",
                "middle_name": "Middle",
                "suffix": "Jr.",
                "birthdate": "2000-01-15",
                "birth_city": "New York",
                "birth_state_province": "NY",
                "birth_country": "USA",
                "school": "Duke",
                "high_school": "Test High",
                "shoots": "R",
                "draft_year": "2024",
                "draft_round": "1",
                "draft_pick": "3",
                "draft_team": "LAL",
                "nba_debut_date": "2024-10-22",
                "nba_debut_season": "2024-25",
                "reference_image_url": "https://example.com/image.jpg",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=created" in response.headers.get("location", "")

        # Verify key fields in database
        result = await db_session.execute(
            text(
                "SELECT display_name, school, draft_team, shoots FROM players_master "
                "WHERE display_name = 'Complete Player'"
            )
        )
        row = result.one()
        assert row[0] == "Complete Player"
        assert row[1] == "Duke"
        assert row[2] == "LAL"
        assert row[3] == "R"


@pytest.mark.asyncio
class TestPlayersEdit:
    """Tests for editing players."""

    async def test_edit_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """GET /admin/players/{id} shows the edit form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}")
        assert response.status_code == 200
        assert "Test Player" in response.text
        assert "Test University" in response.text

    async def test_edit_not_found(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/players/{id} with invalid id returns 404."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/players/99999")
        assert response.status_code == 404

    async def test_update_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id} updates the player."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}",
            data={
                "display_name": "Updated Player",
                "first_name": "Updated",
                "last_name": "Player",
                "school": "Updated University",
                "draft_year": "2025",
                "draft_round": "2",
                "draft_pick": "10",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=updated" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text(
                "SELECT display_name, school, draft_year FROM players_master WHERE id = :id"
            ),
            {"id": sample_player_id},
        )
        row = result.one()
        assert row[0] == "Updated Player"
        assert row[1] == "Updated University"
        assert row[2] == 2025

    async def test_update_missing_required_field_error(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Updating without required field shows error."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}",
            data={
                "display_name": "",
                "first_name": "Test",
                "last_name": "Player",
            },
        )
        assert response.status_code == 200
        assert "required" in response.text.lower()


@pytest.mark.asyncio
class TestPlayersDelete:
    """Tests for deleting players."""

    async def test_delete_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/delete removes the player."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=deleted" in response.headers.get("location", "")

        # Verify removed from database
        result = await db_session.execute(
            text("SELECT id FROM players_master WHERE id = :id"),
            {"id": sample_player_id},
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_not_found(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST /admin/players/{id}/delete with invalid id returns 404."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post("/admin/players/99999/delete")
        assert response.status_code == 404

    async def test_delete_blocked_with_news_items(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
        sample_news_source_id: int,
    ):
        """POST /admin/players/{id}/delete shows error when player has news items."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create a news item linked to the player
        news_item = NewsItem(
            source_id=sample_news_source_id,
            player_id=sample_player_id,
            external_id="player-test-guid",
            title="Player News Article",
            url="https://example.com/player-article",
            tag=NewsItemTag.SCOUTING_REPORT,
            published_at=datetime.utcnow(),
        )
        db_session.add(news_item)
        await db_session.commit()

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/delete"
        )
        assert response.status_code == 200
        assert "Cannot delete" in response.text
        assert "1 linked news item" in response.text

        # Verify player still exists
        result = await db_session.execute(
            text("SELECT id FROM players_master WHERE id = :id"),
            {"id": sample_player_id},
        )
        assert result.scalar_one() == sample_player_id


@pytest.mark.asyncio
class TestSidebarVisibility:
    """Tests for role-based sidebar visibility."""

    async def test_admin_sees_players_link(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Admin sidebar shows Players link."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin")
        assert response.status_code == 200
        assert "/admin/players" in response.text

    async def test_worker_does_not_see_players_link(
        self,
        app_client: AsyncClient,
        worker_user_id: int,
    ):
        """Worker sidebar does not show Players link."""
        _ = worker_user_id
        await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)

        response = await app_client.get("/admin")
        assert response.status_code == 200
        assert "/admin/players" not in response.text

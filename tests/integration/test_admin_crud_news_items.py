"""Integration tests for admin NewsItem CRUD."""

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


ADMIN_EMAIL = "news-items-admin@example.com"
ADMIN_PASSWORD = "admin-password-123"
WORKER_EMAIL = "news-items-worker@example.com"
WORKER_PASSWORD = "worker-password-456"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin auth user for CRUD tests."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )


@pytest_asyncio.fixture
async def worker_user_id(db_session: AsyncSession) -> int:
    """Create a worker auth user for CRUD tests."""
    return await create_auth_user(
        db_session,
        email=WORKER_EMAIL,
        role="worker",
        password=WORKER_PASSWORD,
    )


@pytest_asyncio.fixture
async def sample_source_id(db_session: AsyncSession) -> int:
    """Create a sample news source for news item tests."""
    source = NewsSource(
        name="items-test-source",
        display_name="Items Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/items-test-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    assert source.id is not None
    return source.id


@pytest_asyncio.fixture
async def sample_item_id(db_session: AsyncSession, sample_source_id: int) -> int:
    """Create a sample news item for edit/delete tests."""
    item = NewsItem(
        source_id=sample_source_id,
        external_id="test-guid-123",
        title="Test News Article",
        description="This is a test article description",
        url="https://example.com/test-article",
        author="Test Author",
        tag=NewsItemTag.SCOUTING_REPORT,
        published_at=datetime(2025, 1, 15, 12, 0, 0),
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    assert item.id is not None
    return item.id


@pytest_asyncio.fixture
async def sample_player_id(db_session: AsyncSession) -> int:
    """Create a sample player for association tests."""
    player = PlayerMaster(
        display_name="Test Prospect",
        slug="test-prospect",
        school="Test University",
    )
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)
    assert player.id is not None
    return player.id


@pytest.mark.asyncio
class TestNewsItemsAccess:
    """Tests for access control on news items pages."""

    async def test_list_requires_login(self, app_client: AsyncClient):
        """GET /admin/news-items redirects when not authenticated."""
        response = await app_client.get("/admin/news-items", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")

    async def test_list_requires_admin_role(
        self,
        app_client: AsyncClient,
        worker_user_id: int,
    ):
        """Workers are redirected away from news items."""
        _ = worker_user_id
        await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)

        response = await app_client.get("/admin/news-items", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert response.headers.get("location") == "/admin"

    async def test_list_works_for_admin(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Admins can access the news items list."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-items")
        assert response.status_code == 200
        assert "News Items" in response.text


@pytest.mark.asyncio
class TestNewsItemsList:
    """Tests for the news items list page."""

    async def test_empty_list_shows_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Empty list shows a helpful message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-items")
        assert response.status_code == 200
        assert "No News Items" in response.text

    async def test_list_shows_items(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_item_id: int,
    ):
        """List shows existing news items."""
        _ = admin_user_id
        _ = sample_item_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-items")
        assert response.status_code == 200
        assert "Test News Article" in response.text

    async def test_list_shows_success_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Success query param shows appropriate message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-items?success=updated")
        assert response.status_code == 200
        assert "updated" in response.text.lower()

    async def test_pagination_works(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """Pagination correctly limits and offsets results."""
        _ = admin_user_id

        # Create 5 items
        for i in range(5):
            item = NewsItem(
                source_id=sample_source_id,
                external_id=f"paginate-guid-{i}",
                title=f"Paginated Article {i}",
                url=f"https://example.com/paginate-{i}",
                tag=NewsItemTag.BIG_BOARD,
                published_at=datetime(2025, 1, 15, 12, i, 0),
            )
            db_session.add(item)
        await db_session.commit()

        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Request page with limit=2
        response = await app_client.get("/admin/news-items?limit=2&offset=0")
        assert response.status_code == 200
        assert "Page 1 of 3" in response.text

        # Request page 2
        response = await app_client.get("/admin/news-items?limit=2&offset=2")
        assert response.status_code == 200
        assert "Page 2 of 3" in response.text

    async def test_filter_by_source(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """Filter by source_id works correctly."""
        _ = admin_user_id

        # Create another source
        other_source = NewsSource(
            name="other-source",
            display_name="Other Source",
            feed_type=FeedType.RSS,
            feed_url="https://example.com/other-feed.xml",
            is_active=True,
        )
        db_session.add(other_source)
        await db_session.commit()
        await db_session.refresh(other_source)

        # Create items for each source
        item1 = NewsItem(
            source_id=sample_source_id,
            external_id="source-filter-1",
            title="Article from Source 1",
            url="https://example.com/source1-article",
            tag=NewsItemTag.GAME_RECAP,
            published_at=datetime(2025, 1, 15, 12, 0, 0),
        )
        item2 = NewsItem(
            source_id=other_source.id,
            external_id="source-filter-2",
            title="Article from Source 2",
            url="https://example.com/source2-article",
            tag=NewsItemTag.GAME_RECAP,
            published_at=datetime(2025, 1, 15, 13, 0, 0),
        )
        db_session.add_all([item1, item2])
        await db_session.commit()

        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Filter by sample_source_id
        response = await app_client.get(
            f"/admin/news-items?source_id={sample_source_id}"
        )
        assert response.status_code == 200
        assert "Article from Source 1" in response.text
        assert "Article from Source 2" not in response.text

    async def test_filter_by_tag(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """Filter by tag works correctly."""
        _ = admin_user_id

        # Create items with different tags
        item1 = NewsItem(
            source_id=sample_source_id,
            external_id="tag-filter-1",
            title="Scouting Report Article",
            url="https://example.com/scouting",
            tag=NewsItemTag.SCOUTING_REPORT,
            published_at=datetime(2025, 1, 15, 12, 0, 0),
        )
        item2 = NewsItem(
            source_id=sample_source_id,
            external_id="tag-filter-2",
            title="Mock Draft Article",
            url="https://example.com/mock",
            tag=NewsItemTag.MOCK_DRAFT,
            published_at=datetime(2025, 1, 15, 13, 0, 0),
        )
        db_session.add_all([item1, item2])
        await db_session.commit()

        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Filter by Mock Draft tag
        response = await app_client.get(
            f"/admin/news-items?tag={NewsItemTag.MOCK_DRAFT.value}"
        )
        assert response.status_code == 200
        assert "Mock Draft Article" in response.text
        assert "Scouting Report Article" not in response.text


@pytest.mark.asyncio
class TestNewsItemsEdit:
    """Tests for editing news items."""

    async def test_edit_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_item_id: int,
    ):
        """GET /admin/news-items/{id} shows the edit form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/news-items/{sample_item_id}")
        assert response.status_code == 200
        assert "Test News Article" in response.text
        assert 'name="tag"' in response.text
        assert 'name="player_id"' in response.text
        assert 'name="summary"' in response.text

    async def test_edit_not_found(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/news-items/{id} with invalid id returns 404."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-items/99999")
        assert response.status_code == 404

    async def test_update_tag_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_item_id: int,
    ):
        """POST /admin/news-items/{id} updates the tag."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-items/{sample_item_id}",
            data={
                "tag": NewsItemTag.MOCK_DRAFT.value,
                "player_id": "",
                "summary": "",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=updated" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text("SELECT tag FROM news_items WHERE id = :id"),
            {"id": sample_item_id},
        )
        # SQLAlchemy stores the enum name, not the value
        assert result.scalar_one() == NewsItemTag.MOCK_DRAFT.name

    async def test_update_player_id_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_item_id: int,
        sample_player_id: int,
    ):
        """POST /admin/news-items/{id} updates the player association."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-items/{sample_item_id}",
            data={
                "tag": NewsItemTag.SCOUTING_REPORT.value,
                "player_id": str(sample_player_id),
                "summary": "",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=updated" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text("SELECT player_id FROM news_items WHERE id = :id"),
            {"id": sample_item_id},
        )
        assert result.scalar_one() == sample_player_id

    async def test_update_summary_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_item_id: int,
    ):
        """POST /admin/news-items/{id} updates the summary."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-items/{sample_item_id}",
            data={
                "tag": NewsItemTag.SCOUTING_REPORT.value,
                "player_id": "",
                "summary": "This is a new summary for the article.",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify in database
        result = await db_session.execute(
            text("SELECT summary FROM news_items WHERE id = :id"),
            {"id": sample_item_id},
        )
        assert result.scalar_one() == "This is a new summary for the article."

    async def test_clear_player_id(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_item_id: int,
        sample_player_id: int,
    ):
        """POST /admin/news-items/{id} with empty player_id clears the association."""
        _ = admin_user_id

        # First set player_id
        await db_session.execute(
            text("UPDATE news_items SET player_id = :player_id WHERE id = :id"),
            {"player_id": sample_player_id, "id": sample_item_id},
        )
        await db_session.commit()

        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-items/{sample_item_id}",
            data={
                "tag": NewsItemTag.SCOUTING_REPORT.value,
                "player_id": "",
                "summary": "",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify player_id is cleared
        result = await db_session.execute(
            text("SELECT player_id FROM news_items WHERE id = :id"),
            {"id": sample_item_id},
        )
        assert result.scalar_one() is None


@pytest.mark.asyncio
class TestNewsItemsDelete:
    """Tests for deleting news items."""

    async def test_delete_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_item_id: int,
    ):
        """POST /admin/news-items/{id}/delete removes the item."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-items/{sample_item_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=deleted" in response.headers.get("location", "")

        # Verify removed from database
        result = await db_session.execute(
            text("SELECT id FROM news_items WHERE id = :id"),
            {"id": sample_item_id},
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_not_found(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST /admin/news-items/{id}/delete with invalid id returns 404."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post("/admin/news-items/99999/delete")
        assert response.status_code == 404


@pytest.mark.asyncio
class TestSidebarVisibility:
    """Tests for role-based sidebar visibility."""

    async def test_admin_sees_news_items_link(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Admin sidebar shows News Items link."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin")
        assert response.status_code == 200
        assert "/admin/news-items" in response.text

    async def test_worker_does_not_see_news_items_link(
        self,
        app_client: AsyncClient,
        worker_user_id: int,
    ):
        """Worker sidebar does not show News Items link."""
        _ = worker_user_id
        await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)

        response = await app_client.get("/admin")
        assert response.status_code == 200
        assert "/admin/news-items" not in response.text

"""Integration tests for admin NewsSource CRUD."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "crud-admin@example.com"
ADMIN_PASSWORD = "admin-password-123"
WORKER_EMAIL = "crud-worker@example.com"
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
    """Create a sample news source for edit/delete tests."""
    source = NewsSource(
        name="test-source",
        display_name="Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/test-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    assert source.id is not None
    return source.id


@pytest.mark.asyncio
class TestNewsSourcesAccess:
    """Tests for access control on news sources pages."""

    async def test_list_requires_login(self, app_client: AsyncClient):
        """GET /admin/news-sources redirects when not authenticated."""
        response = await app_client.get("/admin/news-sources", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")

    async def test_list_requires_admin_role(
        self,
        app_client: AsyncClient,
        worker_user_id: int,
    ):
        """Workers are redirected away from news sources."""
        _ = worker_user_id
        await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)

        response = await app_client.get("/admin/news-sources", follow_redirects=False)
        assert response.status_code in {302, 303}
        # Should redirect to dashboard, not show news sources
        assert response.headers.get("location") == "/admin"

    async def test_list_works_for_admin(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Admins can access the news sources list."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-sources")
        assert response.status_code == 200
        assert "News Sources" in response.text


@pytest.mark.asyncio
class TestNewsSourcesList:
    """Tests for the news sources list page."""

    async def test_empty_list_shows_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Empty list shows a helpful message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-sources")
        assert response.status_code == 200
        assert "No News Sources" in response.text or "Add Source" in response.text

    async def test_list_shows_sources(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """List shows existing sources."""
        _ = admin_user_id
        _ = sample_source_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-sources")
        assert response.status_code == 200
        assert "test-source" in response.text or "Test Source" in response.text

    async def test_list_shows_success_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Success query param shows appropriate message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-sources?success=created")
        assert response.status_code == 200
        assert "created" in response.text.lower()


@pytest.mark.asyncio
class TestNewsSourcesCreate:
    """Tests for creating news sources."""

    async def test_new_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/news-sources/new shows the form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-sources/new")
        assert response.status_code == 200
        assert 'name="name"' in response.text
        assert 'name="feed_url"' in response.text

    async def test_create_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """POST /admin/news-sources creates a new source."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/news-sources",
            data={
                "name": "new-source",
                "display_name": "New Source",
                "feed_type": "rss",
                "feed_url": "https://example.com/new-feed.xml",
                "is_active": "1",
                "fetch_interval_minutes": "60",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=created" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text("SELECT name FROM news_sources WHERE name = 'new-source'")
        )
        assert result.scalar_one() == "new-source"

    async def test_create_duplicate_url_error(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """Creating with duplicate feed_url shows error."""
        _ = admin_user_id
        _ = sample_source_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/news-sources",
            data={
                "name": "duplicate-source",
                "display_name": "Duplicate Source",
                "feed_type": "rss",
                "feed_url": "https://example.com/test-feed.xml",  # Same as sample
                "is_active": "1",
                "fetch_interval_minutes": "30",
            },
        )
        assert response.status_code == 200
        assert "already exists" in response.text.lower()


@pytest.mark.asyncio
class TestNewsSourcesEdit:
    """Tests for editing news sources."""

    async def test_edit_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """GET /admin/news-sources/{id} shows the edit form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/news-sources/{sample_source_id}")
        assert response.status_code == 200
        assert "test-source" in response.text
        assert "Test Source" in response.text

    async def test_edit_not_found(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/news-sources/{id} with invalid id returns 404."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/news-sources/99999")
        assert response.status_code == 404

    async def test_update_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """POST /admin/news-sources/{id} updates the source."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-sources/{sample_source_id}",
            data={
                "name": "updated-source",
                "display_name": "Updated Source",
                "feed_type": "rss",
                "feed_url": "https://example.com/updated-feed.xml",
                "is_active": "1",
                "fetch_interval_minutes": "45",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=updated" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text(
                "SELECT name, fetch_interval_minutes FROM news_sources WHERE id = :id"
            ),
            {"id": sample_source_id},
        )
        row = result.one()
        assert row[0] == "updated-source"
        assert row[1] == 45


@pytest.mark.asyncio
class TestNewsSourcesDelete:
    """Tests for deleting news sources."""

    async def test_delete_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """POST /admin/news-sources/{id}/delete removes the source."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/news-sources/{sample_source_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=deleted" in response.headers.get("location", "")

        # Verify removed from database
        result = await db_session.execute(
            text("SELECT id FROM news_sources WHERE id = :id"),
            {"id": sample_source_id},
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_not_found(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST /admin/news-sources/{id}/delete with invalid id returns 404."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post("/admin/news-sources/99999/delete")
        assert response.status_code == 404

    async def test_delete_blocked_with_news_items(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_source_id: int,
    ):
        """POST /admin/news-sources/{id}/delete shows error when source has items."""
        from datetime import UTC, datetime

        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create a news item linked to the source
        news_item = NewsItem(
            source_id=sample_source_id,
            external_id="test-guid",
            title="Test Article",
            url="https://example.com/article",
            tag=NewsItemTag.SCOUTING_REPORT,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db_session.add(news_item)
        await db_session.commit()

        response = await app_client.post(
            f"/admin/news-sources/{sample_source_id}/delete"
        )
        assert response.status_code == 200
        assert "Cannot delete" in response.text
        assert "1 associated news item" in response.text

        # Verify source still exists
        result = await db_session.execute(
            text("SELECT id FROM news_sources WHERE id = :id"),
            {"id": sample_source_id},
        )
        assert result.scalar_one() == sample_source_id


@pytest.mark.asyncio
class TestSidebarVisibility:
    """Tests for role-based sidebar visibility."""

    async def test_admin_sees_news_sources_link(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Admin sidebar shows News Sources link."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin")
        assert response.status_code == 200
        assert "/admin/news-sources" in response.text

    async def test_worker_does_not_see_news_sources_link(
        self,
        app_client: AsyncClient,
        worker_user_id: int,
    ):
        """Worker sidebar does not show News Sources link."""
        _ = worker_user_id
        await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)

        response = await app_client.get("/admin")
        assert response.status_code == 200
        assert "/admin/news-sources" not in response.text

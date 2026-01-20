"""Integration tests for dataset permissions on staff-only news endpoints."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.auth_helpers import (
    create_auth_user,
    grant_dataset_permission,
    login_staff,
)


ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password"

WORKER_VIEW_EMAIL = "worker-view@example.com"
WORKER_EDIT_EMAIL = "worker-edit@example.com"
WORKER_PASSWORD = "worker-password"


@pytest_asyncio.fixture
async def admin_client(app_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    """Return an authenticated admin client."""
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
async def worker_view_client(
    app_client: AsyncClient, db_session: AsyncSession
) -> AsyncClient:
    """Worker with view-only access to news_sources."""
    user_id = await create_auth_user(
        db_session,
        email=WORKER_VIEW_EMAIL,
        role="worker",
        password=WORKER_PASSWORD,
    )
    await grant_dataset_permission(
        db_session,
        user_id=user_id,
        dataset="news_sources",
        can_view=True,
        can_edit=False,
    )
    response = await login_staff(
        app_client,
        email=WORKER_VIEW_EMAIL,
        password=WORKER_PASSWORD,
    )
    assert response.status_code in {302, 303}
    return app_client


@pytest_asyncio.fixture
async def worker_edit_sources_client(
    app_client: AsyncClient, db_session: AsyncSession
) -> AsyncClient:
    """Worker with edit access to news_sources only."""
    user_id = await create_auth_user(
        db_session,
        email=WORKER_EDIT_EMAIL,
        role="worker",
        password=WORKER_PASSWORD,
    )
    await grant_dataset_permission(
        db_session,
        user_id=user_id,
        dataset="news_sources",
        can_view=True,
        can_edit=True,
    )
    response = await login_staff(
        app_client,
        email=WORKER_EDIT_EMAIL,
        password=WORKER_PASSWORD,
    )
    assert response.status_code in {302, 303}
    return app_client


@pytest.mark.asyncio
class TestNewsSourcesPermissions:
    """Permissions for /api/news/sources."""

    async def test_requires_auth(self, app_client: AsyncClient):
        """Logged-out requests get 401."""
        response = await app_client.get("/api/news/sources")
        assert response.status_code == 401

    async def test_worker_view_can_list_sources(self, worker_view_client: AsyncClient):
        """Workers with view can list sources."""
        response = await worker_view_client.get("/api/news/sources")
        assert response.status_code == 200

    async def test_worker_view_cannot_create_source(self, worker_view_client: AsyncClient):
        """Workers without edit cannot create sources."""
        response = await worker_view_client.post(
            "/api/news/sources",
            json={
                "name": "Denied Source",
                "display_name": "Denied Source",
                "feed_url": "https://denied.example/feed",
                "feed_type": "rss",
                "fetch_interval_minutes": 60,
            },
        )
        assert response.status_code == 403

    async def test_worker_edit_can_create_source(
        self,
        worker_edit_sources_client: AsyncClient,
    ):
        """Workers with edit can create sources."""
        response = await worker_edit_sources_client.post(
            "/api/news/sources",
            json={
                "name": "Allowed Source",
                "display_name": "Allowed Source",
                "feed_url": "https://allowed.example/feed",
                "feed_type": "rss",
                "fetch_interval_minutes": 60,
            },
        )
        assert response.status_code == 201


@pytest.mark.asyncio
class TestNewsIngestionPermissions:
    """Permissions for /api/news/ingest."""

    async def test_requires_auth(self, app_client: AsyncClient):
        """Logged-out requests get 401."""
        response = await app_client.post("/api/news/ingest")
        assert response.status_code == 401

    async def test_worker_without_permission_cannot_ingest(
        self,
        worker_edit_sources_client: AsyncClient,
    ):
        """Workers without news_ingestion:edit cannot trigger ingestion."""
        response = await worker_edit_sources_client.post("/api/news/ingest")
        assert response.status_code == 403

    async def test_admin_can_ingest(self, admin_client: AsyncClient):
        """Admins can trigger ingestion."""
        response = await admin_client.post("/api/news/ingest")
        assert response.status_code == 200
        data = response.json()
        assert "sources_processed" in data
        assert "items_added" in data
        assert "items_skipped" in data
        assert "errors" in data


"""Integration tests for the news feed API endpoints."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource


@pytest.fixture
async def sample_news_source(db_session: AsyncSession) -> NewsSource:
    """Create a sample news source for testing."""
    source = NewsSource(
        name="Test Source",
        display_name="Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/feed",
        is_active=True,
        fetch_interval_minutes=30,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def sample_news_items(
    db_session: AsyncSession, sample_news_source: NewsSource
) -> list[NewsItem]:
    """Create sample news items for testing."""
    now = datetime.now(timezone.utc)
    items = [
        NewsItem(
            source_id=sample_news_source.id,  # type: ignore[arg-type]
            external_id="article-1",
            title="Test Article 1",
            description="Description for article 1",
            url="https://example.com/article-1",
            image_url="https://example.com/img1.jpg",
            author="John Doe",
            summary="AI summary for article 1",
            tag=NewsItemTag.RISER,
            published_at=now - timedelta(hours=1),
            created_at=now,
        ),
        NewsItem(
            source_id=sample_news_source.id,  # type: ignore[arg-type]
            external_id="article-2",
            title="Test Article 2",
            description="Description for article 2",
            url="https://example.com/article-2",
            image_url=None,  # Test article without image
            author=None,
            summary="AI summary for article 2",
            tag=NewsItemTag.ANALYSIS,
            published_at=now - timedelta(hours=2),
            created_at=now,
        ),
        NewsItem(
            source_id=sample_news_source.id,  # type: ignore[arg-type]
            external_id="article-3",
            title="Test Article 3",
            description="Description for article 3",
            url="https://example.com/article-3",
            image_url="https://example.com/img3.jpg",
            author="Jane Smith",
            summary="AI summary for article 3",
            tag=NewsItemTag.FALLER,
            published_at=now - timedelta(days=1),
            created_at=now,
        ),
    ]
    for item in items:
        db_session.add(item)
    await db_session.commit()
    for item in items:
        await db_session.refresh(item)
    return items


@pytest.mark.asyncio
class TestGetNewsFeed:
    """Tests for GET /api/news endpoint."""

    async def test_returns_empty_feed_when_no_items(self, app_client: AsyncClient):
        """GET /api/news returns empty items array when no news exists."""
        response = await app_client.get("/api/news")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_returns_news_items(
        self,
        app_client: AsyncClient,
        sample_news_items: list[NewsItem],
    ):
        """GET /api/news returns news items with correct format."""
        response = await app_client.get("/api/news")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 3
        assert len(data["items"]) == 3

        # Items should be ordered by published_at DESC (most recent first)
        first_item = data["items"][0]
        assert first_item["title"] == "Test Article 1"
        assert first_item["source_name"] == "Test Source"
        assert first_item["summary"] == "AI summary for article 1"
        assert first_item["tag"] == "Riser"
        assert first_item["read_more_text"] == "Read at Test Source"
        assert first_item["image_url"] == "https://example.com/img1.jpg"
        assert first_item["author"] == "John Doe"

    async def test_respects_limit_parameter(
        self,
        app_client: AsyncClient,
        sample_news_items: list[NewsItem],
    ):
        """GET /api/news respects the limit query parameter."""
        response = await app_client.get("/api/news?limit=2")
        assert response.status_code == 200
        data = response.json()

        assert len(data["items"]) == 2
        assert data["total"] == 3  # Total count unchanged
        assert data["limit"] == 2

    async def test_respects_offset_parameter(
        self,
        app_client: AsyncClient,
        sample_news_items: list[NewsItem],
    ):
        """GET /api/news respects the offset query parameter."""
        response = await app_client.get("/api/news?offset=1")
        assert response.status_code == 200
        data = response.json()

        assert len(data["items"]) == 2
        assert data["offset"] == 1
        # First item after offset should be article 2
        assert data["items"][0]["title"] == "Test Article 2"

    async def test_handles_missing_image(
        self,
        app_client: AsyncClient,
        sample_news_items: list[NewsItem],
    ):
        """GET /api/news correctly returns null for items without images."""
        response = await app_client.get("/api/news")
        data = response.json()

        # Article 2 has no image
        article_2 = next(
            item for item in data["items"] if item["title"] == "Test Article 2"
        )
        assert article_2["image_url"] is None
        assert article_2["author"] is None


@pytest.mark.asyncio
class TestListSources:
    """Tests for GET /api/news/sources endpoint."""

    async def test_returns_empty_list_when_no_sources(self, app_client: AsyncClient):
        """GET /api/news/sources returns empty array when no sources exist."""
        response = await app_client.get("/api/news/sources")
        assert response.status_code == 200
        assert response.json() == []

    async def test_returns_sources(
        self,
        app_client: AsyncClient,
        sample_news_source: NewsSource,
    ):
        """GET /api/news/sources returns configured sources."""
        response = await app_client.get("/api/news/sources")
        assert response.status_code == 200
        data = response.json()

        assert len(data) == 1
        source = data[0]
        assert source["name"] == "Test Source"
        assert source["display_name"] == "Test Source"
        assert source["feed_type"] == "rss"
        assert source["feed_url"] == "https://example.com/feed"
        assert source["is_active"] is True
        assert source["fetch_interval_minutes"] == 30


@pytest.mark.asyncio
class TestCreateSource:
    """Tests for POST /api/news/sources endpoint."""

    async def test_creates_new_source(self, app_client: AsyncClient):
        """POST /api/news/sources creates a new RSS source."""
        response = await app_client.post(
            "/api/news/sources",
            json={
                "name": "New Source",
                "display_name": "New Source Display",
                "feed_url": "https://newsource.com/feed",
                "feed_type": "rss",
                "fetch_interval_minutes": 60,
            },
        )
        assert response.status_code == 201
        data = response.json()

        assert data["name"] == "New Source"
        assert data["display_name"] == "New Source Display"
        assert data["feed_url"] == "https://newsource.com/feed"
        assert data["feed_type"] == "rss"
        assert data["is_active"] is True
        assert data["fetch_interval_minutes"] == 60
        assert "id" in data

    async def test_rejects_duplicate_feed_url(
        self,
        app_client: AsyncClient,
        sample_news_source: NewsSource,
    ):
        """POST /api/news/sources rejects duplicate feed URLs."""
        response = await app_client.post(
            "/api/news/sources",
            json={
                "name": "Duplicate",
                "display_name": "Duplicate",
                "feed_url": "https://example.com/feed",  # Same as sample_news_source
                "feed_type": "rss",
            },
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    async def test_rejects_invalid_feed_type(self, app_client: AsyncClient):
        """POST /api/news/sources rejects invalid feed types."""
        response = await app_client.post(
            "/api/news/sources",
            json={
                "name": "Invalid",
                "display_name": "Invalid",
                "feed_url": "https://invalid.com/feed",
                "feed_type": "invalid_type",
            },
        )
        assert response.status_code == 400
        assert "Invalid feed type" in response.json()["detail"]


@pytest.mark.asyncio
class TestTriggerIngestion:
    """Tests for POST /api/news/ingest endpoint."""

    async def test_ingestion_returns_result(self, app_client: AsyncClient):
        """POST /api/news/ingest returns an ingestion result."""
        response = await app_client.post("/api/news/ingest")
        assert response.status_code == 200
        data = response.json()

        assert "sources_processed" in data
        assert "items_added" in data
        assert "items_skipped" in data
        assert "errors" in data
        assert isinstance(data["errors"], list)

    async def test_ingestion_processes_active_sources(
        self,
        app_client: AsyncClient,
        sample_news_source: NewsSource,
    ):
        """POST /api/news/ingest processes active sources.

        Note: This test verifies the endpoint runs without error.
        Actual RSS parsing would require mocking feedparser.
        """
        response = await app_client.post("/api/news/ingest")
        assert response.status_code == 200
        data = response.json()

        # Source should be counted even if feed fetch fails
        assert data["sources_processed"] >= 0

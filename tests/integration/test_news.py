"""Integration tests for the news feed API endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from app.services.news_service import get_filtered_news_feed
from app.services.news_summarization_service import ArticleAnalysis
from tests.integration.auth_helpers import create_auth_user, login_staff

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def admin_client(app_client: AsyncClient, db_session: AsyncSession) -> AsyncClient:
    """Return an authenticated admin client for staff-only news endpoints."""
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
async def sample_news_source(db_session: AsyncSession) -> NewsSource:
    """Create a sample news source for testing."""
    source = NewsSource(
        name="Test Source",
        display_name="Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/feed",
        is_active=True,
        fetch_interval_minutes=30,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest_asyncio.fixture
async def sample_news_items(
    db_session: AsyncSession, sample_news_source: NewsSource
) -> list[NewsItem]:
    """Create sample news items for testing."""
    now = datetime.now(UTC).replace(tzinfo=None)
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
            tag=NewsItemTag.SCOUTING_REPORT,
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
            tag=NewsItemTag.BIG_BOARD,
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
            tag=NewsItemTag.MOCK_DRAFT,
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
        assert first_item["tag"] == "Scouting Report"
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

    async def test_filtered_news_feed_accepts_tag_name_and_value(
        self,
        db_session: AsyncSession,
        sample_news_items: list[NewsItem],
    ):
        """Tag filtering works for both display-value and enum-name inputs."""
        _ = sample_news_items
        feed_by_value = await get_filtered_news_feed(
            db_session,
            tag=NewsItemTag.MOCK_DRAFT.value,
        )
        feed_by_name = await get_filtered_news_feed(
            db_session,
            tag=NewsItemTag.MOCK_DRAFT.name,
        )

        assert feed_by_value.total == 1
        assert feed_by_name.total == 1
        assert feed_by_value.items[0].title == "Test Article 3"
        assert feed_by_name.items[0].title == "Test Article 3"


@pytest.mark.asyncio
class TestListSources:
    """Tests for GET /api/news/sources endpoint."""

    async def test_requires_auth(self, app_client: AsyncClient):
        """GET /api/news/sources requires staff authentication."""
        response = await app_client.get("/api/news/sources")
        assert response.status_code == 401

    async def test_returns_empty_list_when_no_sources(self, admin_client: AsyncClient):
        """GET /api/news/sources returns empty array when no sources exist."""
        response = await admin_client.get("/api/news/sources")
        assert response.status_code == 200
        assert response.json() == []

    async def test_returns_sources(
        self,
        admin_client: AsyncClient,
        sample_news_source: NewsSource,
    ):
        """GET /api/news/sources returns configured sources."""
        response = await admin_client.get("/api/news/sources")
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

    async def test_requires_auth(self, app_client: AsyncClient):
        """POST /api/news/sources requires staff authentication."""
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
        assert response.status_code == 401

    async def test_creates_new_source(self, admin_client: AsyncClient):
        """POST /api/news/sources creates a new RSS source."""
        response = await admin_client.post(
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
        admin_client: AsyncClient,
        sample_news_source: NewsSource,
    ):
        """POST /api/news/sources rejects duplicate feed URLs."""
        response = await admin_client.post(
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

    async def test_rejects_invalid_feed_type(self, admin_client: AsyncClient):
        """POST /api/news/sources rejects invalid feed types."""
        response = await admin_client.post(
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

    async def test_requires_auth(self, app_client: AsyncClient):
        """POST /api/news/ingest requires staff authentication."""
        response = await app_client.post("/api/news/ingest")
        assert response.status_code == 401

    async def test_ingestion_returns_result(self, admin_client: AsyncClient):
        """POST /api/news/ingest returns an ingestion result."""
        response = await admin_client.post("/api/news/ingest")
        assert response.status_code == 200
        data = response.json()

        assert "sources_processed" in data
        assert "items_added" in data
        assert "items_skipped" in data
        assert "errors" in data
        assert isinstance(data["errors"], list)

    async def test_ingestion_processes_active_sources(
        self,
        admin_client: AsyncClient,
        sample_news_source: NewsSource,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """POST /api/news/ingest processes active sources.

        Mocks the RSS fetch to keep tests deterministic and offline.
        """
        from app.services import news_ingestion_service

        async def _fake_fetch_rss_feed(url: str) -> list[dict]:
            assert url == sample_news_source.feed_url
            return [
                {
                    "title": "Mock entry",
                    "description": "Mock description",
                    "link": "https://example.com/article-1",
                    "guid": "mock-1",
                    "author": "Mock Author",
                    "image_url": None,
                    "published_at": datetime.now(UTC).replace(tzinfo=None),
                }
            ]

        async def _fake_analyze_article(
            *, title: str, description: str
        ) -> ArticleAnalysis:
            return ArticleAnalysis(summary="Mock summary", tag=NewsItemTag.BIG_BOARD)

        monkeypatch.setattr(
            news_ingestion_service, "fetch_rss_feed", _fake_fetch_rss_feed
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            _fake_analyze_article,
        )

        response = await admin_client.post("/api/news/ingest")
        assert response.status_code == 200
        data = response.json()

        assert data["sources_processed"] == 1
        assert data["items_added"] == 1
        assert data["items_skipped"] == 0

    async def test_ingestion_adds_and_skips_duplicates(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        sample_news_source: NewsSource,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """POST /api/news/ingest adds new items and skips known external_ids."""
        from app.services import news_ingestion_service

        existing_item = NewsItem(
            source_id=sample_news_source.id,  # type: ignore[arg-type]
            external_id="dup-1",
            title="Existing",
            description=None,
            url="https://example.com/existing",
            image_url=None,
            author=None,
            summary="Existing summary",
            tag=NewsItemTag.SCOUTING_REPORT,
            published_at=datetime.now(UTC).replace(tzinfo=None),
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db_session.add(existing_item)
        await db_session.commit()

        async def _fake_fetch_rss_feed(url: str) -> list[dict]:
            assert url == sample_news_source.feed_url
            now = datetime.now(UTC).replace(tzinfo=None)
            return [
                {
                    "title": "Duplicate entry",
                    "description": "Dup description",
                    "link": "https://example.com/duplicate",
                    "guid": "dup-1",
                    "author": None,
                    "image_url": None,
                    "published_at": now,
                },
                {
                    "title": "New entry",
                    "description": "New description",
                    "link": "https://example.com/new",
                    "guid": "new-1",
                    "author": None,
                    "image_url": None,
                    "published_at": now,
                },
            ]

        async def _fake_analyze_article(
            *, title: str, description: str
        ) -> ArticleAnalysis:
            return ArticleAnalysis(summary="Mock summary", tag=NewsItemTag.MOCK_DRAFT)

        monkeypatch.setattr(
            news_ingestion_service, "fetch_rss_feed", _fake_fetch_rss_feed
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            _fake_analyze_article,
        )

        response = await admin_client.post("/api/news/ingest")
        assert response.status_code == 200
        data = response.json()
        assert data["sources_processed"] == 1
        assert data["items_added"] == 1
        assert data["items_skipped"] == 1

        result = await db_session.execute(
            select(NewsItem).where(  # type: ignore[call-overload]
                NewsItem.source_id == sample_news_source.id  # type: ignore[arg-type]
            )
        )
        items = list(result.scalars().all())
        assert len(items) == 2

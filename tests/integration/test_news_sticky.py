"""Integration tests for the news sticky/pinned-post feature.

Covers the service layer (`get_sticky_news_item`, `set_sticky_news_item`),
the homepage and /news rendering paths, and the admin POST handler that
toggles the sticky flag while enforcing the single-sticky invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from app.services.news_service import (
    get_sticky_news_item,
    set_sticky_news_item,
)
from tests.integration.auth_helpers import create_auth_user, login_staff

ADMIN_EMAIL = "sticky-admin@example.com"
ADMIN_PASSWORD = "sticky-pass-123"


@pytest_asyncio.fixture
async def source(db_session: AsyncSession) -> NewsSource:
    row = NewsSource(
        name="sticky-source",
        display_name="Sticky Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/sticky-feed",
        is_active=True,
        fetch_interval_minutes=30,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def items(db_session: AsyncSession, source: NewsSource) -> list[NewsItem]:
    """Three plain news items, none sticky."""
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        NewsItem(
            source_id=source.id,  # type: ignore[arg-type]
            external_id=f"sticky-item-{i}",
            title=f"Sticky Test Article {i}",
            url=f"https://example.com/sticky-{i}",
            tag=NewsItemTag.SCOUTING_REPORT,
            published_at=now - timedelta(hours=i),
            created_at=now,
        )
        for i in range(1, 4)
    ]
    for r in rows:
        db_session.add(r)
    await db_session.commit()
    for r in rows:
        await db_session.refresh(r)
    return rows


@pytest.mark.asyncio
class TestStickyService:
    """Service-level: fetch + set sticky."""

    async def test_returns_none_when_no_sticky(
        self,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """get_sticky_news_item returns None when no row has is_sticky=True."""
        _ = items
        assert await get_sticky_news_item(db_session) is None

    async def test_set_then_fetch_sticky(
        self,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Setting sticky on an item makes it returnable via the fetch helper."""
        target = items[1]
        await set_sticky_news_item(db_session, target.id)
        await db_session.commit()

        sticky = await get_sticky_news_item(db_session)
        assert sticky is not None
        assert sticky.id == target.id
        assert sticky.is_sticky is True

    async def test_set_sticky_unpins_prior(
        self,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Setting a new sticky must unset the prior sticky (single-row invariant)."""
        first, second, _ = items
        await set_sticky_news_item(db_session, first.id)
        await db_session.commit()

        await set_sticky_news_item(db_session, second.id)
        await db_session.commit()

        # Only `second` should be sticky now.
        result = await db_session.execute(
            select(NewsItem.id).where(NewsItem.is_sticky.is_(True))  # type: ignore[attr-defined,call-overload]
        )
        sticky_ids = [row[0] for row in result.all()]
        assert sticky_ids == [second.id]

    async def test_set_sticky_none_clears(
        self,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Passing item_id=None clears the sticky entirely."""
        await set_sticky_news_item(db_session, items[0].id)
        await db_session.commit()

        await set_sticky_news_item(db_session, None)
        await db_session.commit()

        assert await get_sticky_news_item(db_session) is None


@pytest.mark.asyncio
class TestStickyOnPublicPages:
    """Verify the public homepage and /news prepend + dedup the sticky."""

    async def test_homepage_renders_pinned_label_for_sticky(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """When a sticky is set, the homepage payload marks it is_sticky."""
        target = items[2]  # Oldest one — proves it surfaces despite ordering.
        await set_sticky_news_item(db_session, target.id)
        await db_session.commit()

        response = await app_client.get("/")
        assert response.status_code == 200
        # The feed_items list is serialized into a window.FEED_ITEMS literal;
        # the sticky item should be there with is_sticky=true and appear first.
        body = response.text
        # Article 3 is the oldest, so without sticky it would be last. With
        # sticky, it should still be present (and marked sticky in the JSON
        # blob). We assert presence + a sticky marker rather than parse JS.
        assert "Sticky Test Article 3" in body
        assert '"is_sticky": true' in body

    async def test_news_page_pins_when_no_filter(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """/news with no filters prepends the sticky."""
        target = items[2]
        await set_sticky_news_item(db_session, target.id)
        await db_session.commit()

        response = await app_client.get("/news")
        assert response.status_code == 200
        body = response.text
        assert "Sticky Test Article 3" in body
        assert '"is_sticky": true' in body

    async def test_news_page_hides_sticky_when_filtered(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
        source: NewsSource,
    ):
        """A filter on /news suppresses the sticky pin (cleaner mental model).

        The sticky item could still appear if it organically matches the
        filter, but it should NOT be flagged is_sticky=true. We pick a tag
        the sticky item does not have.
        """
        # Sticky article has tag=SCOUTING_REPORT; filter to MOCK_DRAFT.
        target = items[0]  # SCOUTING_REPORT
        await set_sticky_news_item(db_session, target.id)
        await db_session.commit()

        response = await app_client.get(
            f"/news?tag={NewsItemTag.MOCK_DRAFT.value}"
        )
        assert response.status_code == 200
        body = response.text
        # The sticky article shouldn't be there (wrong tag), and no
        # is_sticky=true marker should be emitted.
        assert "Sticky Test Article 1" not in body
        assert '"is_sticky": true' not in body

    async def test_news_page_pagination_skips_sticky(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Page 2 of /news must not re-render the sticky."""
        target = items[0]
        await set_sticky_news_item(db_session, target.id)
        await db_session.commit()

        # Offset > 0 means "not the first page" — sticky must be hidden.
        response = await app_client.get("/news?offset=12")
        assert response.status_code == 200
        assert '"is_sticky": true' not in response.text


@pytest.mark.asyncio
class TestStickyAdminToggle:
    """Admin form: checkbox toggles the sticky flag with invariant enforcement."""

    async def _login_admin(
        self, app_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await create_auth_user(
            db_session,
            email=ADMIN_EMAIL,
            role="admin",
            password=ADMIN_PASSWORD,
        )
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

    async def test_admin_pin_sets_is_sticky(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Submitting the edit form with is_sticky=on pins the item."""
        await self._login_admin(app_client, db_session)
        target = items[1]

        response = await app_client.post(
            f"/admin/news-items/{target.id}",
            data={
                "tag": NewsItemTag.SCOUTING_REPORT.value,
                "player_id": "",
                "summary": "",
                "is_sticky": "on",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        result = await db_session.execute(
            text("SELECT is_sticky FROM news_items WHERE id = :id"),
            {"id": target.id},
        )
        assert result.scalar_one() is True

    async def test_admin_unchecked_clears_existing_sticky(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Submitting without the is_sticky field unpins a currently-sticky item."""
        await self._login_admin(app_client, db_session)
        target = items[0]
        await set_sticky_news_item(db_session, target.id)
        await db_session.commit()

        response = await app_client.post(
            f"/admin/news-items/{target.id}",
            data={
                "tag": NewsItemTag.SCOUTING_REPORT.value,
                "player_id": "",
                "summary": "",
                # is_sticky intentionally omitted, like an unchecked checkbox
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        result = await db_session.execute(
            text("SELECT is_sticky FROM news_items WHERE id = :id"),
            {"id": target.id},
        )
        assert result.scalar_one() is False

    async def test_admin_pin_enforces_single_sticky(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        items: list[NewsItem],
    ):
        """Pinning a second item via admin auto-unpins the first."""
        await self._login_admin(app_client, db_session)
        first, second, _ = items

        # Pin the first item.
        await set_sticky_news_item(db_session, first.id)
        await db_session.commit()

        # Pin the second via the admin POST.
        response = await app_client.post(
            f"/admin/news-items/{second.id}",
            data={
                "tag": NewsItemTag.SCOUTING_REPORT.value,
                "player_id": "",
                "summary": "",
                "is_sticky": "on",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        result = await db_session.execute(
            text("SELECT id FROM news_items WHERE is_sticky = true ORDER BY id")
        )
        sticky_ids = [row[0] for row in result.all()]
        assert sticky_ids == [second.id]

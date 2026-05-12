"""Integration tests for the news ingestion relevance gate."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from app.services import news_ingestion_service, publisher_filters
from app.services.news_ingestion_service import (
    NewsSourceSnapshot,
    ingest_rss_source,
)
from app.services.news_summarization_service import ArticleAnalysis


def _entry(
    guid: str,
    title: str,
    description: str = "",
    link: str | None = None,
) -> dict[str, Any]:
    """Build a normalized RSS entry dict matching fetch_rss_feed output."""
    return {
        "title": title,
        "description": description,
        "link": link or f"https://example.com/{guid}",
        "guid": guid,
        "author": None,
        "image_url": None,
        "published_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


def _stub_analysis(*_args: Any, **_kwargs: Any) -> ArticleAnalysis:
    return ArticleAnalysis(
        summary="A summary",
        tag=NewsItemTag.SCOUTING_REPORT,
        mentioned_players=[],
    )


async def _make_source(
    db_session: AsyncSession,
    *,
    name: str,
    feed_url: str,
    is_draft_focused: bool,
) -> NewsSource:
    source = NewsSource(
        name=name,
        display_name=name,
        feed_type=FeedType.RSS,
        feed_url=feed_url,
        is_active=True,
        fetch_interval_minutes=30,
        is_draft_focused=is_draft_focused,
    )
    db_session.add(source)
    await db_session.flush()
    await db_session.commit()
    assert source.id is not None
    return source


def _snapshot(source: NewsSource) -> NewsSourceSnapshot:
    assert source.id is not None
    return NewsSourceSnapshot(
        id=source.id,
        name=source.name,
        feed_type=source.feed_type,
        feed_url=source.feed_url,
        is_draft_focused=source.is_draft_focused,
    )


@pytest.mark.asyncio
class TestIngestionRelevanceGate:
    """Tests for the per-source relevance gate in news ingestion."""

    async def test_draft_focused_skips_relevance_check(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """is_draft_focused=True skips the relevance gate entirely."""
        source = await _make_source(
            db_session,
            name="draft-focused",
            feed_url="https://example.com/df.xml",
            is_draft_focused=True,
        )

        relevance_calls: list[tuple[str, str]] = []

        async def fake_relevance(title: str, description: str) -> bool:
            relevance_calls.append((title, description))
            return False

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [_entry("g-1", "Random topic with no draft keywords")]

        monkeypatch.setattr(
            news_ingestion_service,
            "fetch_rss_feed",
            fake_fetch,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            _stub_analysis,
        )

        added, skipped, filtered, mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert relevance_calls == []
        assert added == 1
        assert filtered == 0
        rows = (
            (
                await db_session.execute(
                    select(NewsItem).where(NewsItem.source_id == source.id)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def test_keyword_match_short_circuits_gemini(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keyword pre-filter alone is enough to admit an article."""
        source = await _make_source(
            db_session,
            name="silver-bulletin-kw",
            feed_url="https://example.com/sb-kw.xml",
            is_draft_focused=False,
        )

        relevance_calls: list[tuple[str, str]] = []

        async def fake_relevance(title: str, description: str) -> bool:
            relevance_calls.append((title, description))
            return False

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [_entry("g-2", "2025 Mock Draft Update", "Latest tier moves")]

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            _stub_analysis,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert relevance_calls == []  # keyword hit short-circuited Gemini
        assert added == 1
        assert filtered == 0

    async def test_gemini_says_relevant_admits_article(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No keyword hit + Gemini-relevant ⇒ article is admitted."""
        source = await _make_source(
            db_session,
            name="silver-bulletin-pos",
            feed_url="https://example.com/sb-pos.xml",
            is_draft_focused=False,
        )

        async def fake_relevance(_title: str, _desc: str) -> bool:
            return True

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [_entry("g-3", "An ambiguous title", "no obvious keywords here")]

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            _stub_analysis,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert added == 1
        assert filtered == 0

    async def test_gemini_says_irrelevant_filters_article(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No keyword hit + Gemini-not-relevant ⇒ article is filtered out.

        Filtered items don't reach the DB and aren't counted as added.
        """
        source = await _make_source(
            db_session,
            name="silver-bulletin-neg",
            feed_url="https://example.com/sb-neg.xml",
            is_draft_focused=False,
        )

        analyze_calls: list[tuple[str, str]] = []

        async def fake_relevance(_title: str, _desc: str) -> bool:
            return False

        async def fake_analyze(title: str, description: str) -> ArticleAnalysis:
            analyze_calls.append((title, description))
            return _stub_analysis()

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [
                _entry("g-4-keep", "Mock Draft Madness", "On-topic content"),
                _entry("g-4-drop", "Election polling deep dive", "polling models"),
            ]

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            fake_analyze,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert added == 1
        assert filtered == 1
        # Only the keyword-matched article should hit analyze_article
        assert len(analyze_calls) == 1
        assert analyze_calls[0][0] == "Mock Draft Madness"

        rows = (
            (
                await db_session.execute(
                    select(NewsItem).where(NewsItem.source_id == source.id)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].external_id == "g-4-keep"


@pytest.mark.asyncio
class TestSilverBulletinPublisherFilter:
    """Silver Bulletin honors a no-methodology / no-models-and-forecasts rule.

    The filter activates by feed_url host (``natesilver.net``) and applies a
    union of two checks: URL slug substring ``methodology`` and Substack
    ``section_slug == "models-and-forecasts"``. Excluded entries are counted
    against ``items_filtered`` and never reach the relevance gate or AI
    analysis.
    """

    async def test_methodology_slug_is_filtered(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Posts whose URL slug contains 'methodology' are dropped."""
        source = await _make_source(
            db_session,
            name="silver-bulletin-meth",
            feed_url="https://www.natesilver.net/feed",
            is_draft_focused=False,
        )

        analyze_calls: list[str] = []
        relevance_calls: list[str] = []

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [
                _entry(
                    "g-meth",
                    "How we calculate our PELE ratings",
                    "Deep dive on the model",
                    link="https://www.natesilver.net/p/pele-methodology",
                ),
            ]

        async def fake_archive() -> dict[str, str]:
            return {}  # archive empty -- slug filter alone must catch it

        async def fake_relevance(title: str, _desc: str) -> bool:
            relevance_calls.append(title)
            return True

        async def fake_analyze(title: str, description: str) -> ArticleAnalysis:
            analyze_calls.append(title)
            return _stub_analysis()

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            publisher_filters, "_fetch_silver_bulletin_archive", fake_archive
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            fake_analyze,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert added == 0
        assert filtered == 1
        assert analyze_calls == []
        assert relevance_calls == []

    async def test_models_and_forecasts_section_is_filtered(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Posts whose Substack section_slug is 'models-and-forecasts' are dropped."""
        source = await _make_source(
            db_session,
            name="silver-bulletin-mf",
            feed_url="https://www.natesilver.net/feed",
            is_draft_focused=False,
        )

        dashboard_url = (
            "https://www.natesilver.net/p/trump-approval-ratings-nate-silver-bulletin"
        )
        analyze_calls: list[str] = []

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [
                _entry(
                    "g-mf",
                    "How popular is Donald Trump?",
                    "Live tracker",
                    link=dashboard_url,
                ),
            ]

        async def fake_archive() -> dict[str, str]:
            return {dashboard_url: "models-and-forecasts"}

        async def fake_relevance(_title: str, _desc: str) -> bool:
            return True

        async def fake_analyze(title: str, description: str) -> ArticleAnalysis:
            analyze_calls.append(title)
            return _stub_analysis()

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            publisher_filters, "_fetch_silver_bulletin_archive", fake_archive
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            fake_analyze,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert added == 0
        assert filtered == 1
        assert analyze_calls == []

    async def test_other_section_passes_through_to_relevance_gate(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Posts outside excluded sections still hit the relevance gate.

        A sports-section post with a draft keyword should be admitted.
        """
        source = await _make_source(
            db_session,
            name="silver-bulletin-pass",
            feed_url="https://www.natesilver.net/feed",
            is_draft_focused=False,
        )

        draft_url = (
            "https://www.natesilver.net/p/"
            "radical-plan-to-replace-the-nba-draft-lottery-arc-auction"
        )
        analyze_calls: list[str] = []

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [
                _entry(
                    "g-draft",
                    "Our radical plan to replace the NBA draft lottery",
                    "Auction-based draft order",
                    link=draft_url,
                ),
            ]

        async def fake_archive() -> dict[str, str]:
            return {draft_url: "sports"}

        async def fake_relevance(_title: str, _desc: str) -> bool:
            return True

        async def fake_analyze(title: str, description: str) -> ArticleAnalysis:
            analyze_calls.append(title)
            return _stub_analysis()

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            publisher_filters, "_fetch_silver_bulletin_archive", fake_archive
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            fake_analyze,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert added == 1
        assert filtered == 0
        assert len(analyze_calls) == 1

    async def test_archive_failure_still_filters_methodology_slug(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Archive API failure → section filter is bypassed but slug filter still runs.

        Defense-in-depth: section info is the secondary layer. Failing the
        archive lookup must not blackhole the entire feed.
        """
        source = await _make_source(
            db_session,
            name="silver-bulletin-fallback",
            feed_url="https://www.natesilver.net/feed",
            is_draft_focused=False,
        )

        meth_url = "https://www.natesilver.net/p/sbcb-methodology"
        draft_url = (
            "https://www.natesilver.net/p/"
            "radical-plan-to-replace-the-nba-draft-lottery-arc-auction"
        )
        analyze_calls: list[str] = []

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [
                _entry(
                    "g-meth-fb",
                    "SBCB methodology",
                    "How our model works",
                    link=meth_url,
                ),
                _entry(
                    "g-draft-fb",
                    "NBA draft lottery proposal",
                    "Auction-based fix",
                    link=draft_url,
                ),
            ]

        async def fake_archive() -> dict[str, str]:
            return {}  # simulate API failure

        async def fake_relevance(_title: str, _desc: str) -> bool:
            return True

        async def fake_analyze(title: str, description: str) -> ArticleAnalysis:
            analyze_calls.append(title)
            return _stub_analysis()

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            publisher_filters, "_fetch_silver_bulletin_archive", fake_archive
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            fake_analyze,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        # Methodology dropped by slug filter; draft post admitted.
        assert added == 1
        assert filtered == 1
        assert analyze_calls == ["NBA draft lottery proposal"]

    async def test_non_silver_bulletin_source_is_untouched(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Filter must not fire for sources whose feed URL is not natesilver.net.

        A post whose URL coincidentally contains the substring 'methodology'
        on a non-Silver-Bulletin feed should still be admitted (or be left to
        the relevance gate).
        """
        source = await _make_source(
            db_session,
            name="other-source",
            feed_url="https://other-publisher.com/feed",
            is_draft_focused=False,
        )

        archive_calls: list[None] = []

        async def fake_fetch(_url: str) -> list[dict[str, Any]]:
            return [
                _entry(
                    "g-other-meth",
                    "Our NBA Draft methodology",
                    "Mock draft tier methodology",
                    link="https://other-publisher.com/posts/draft-methodology",
                ),
            ]

        async def fake_archive() -> dict[str, str]:
            archive_calls.append(None)
            return {}

        async def fake_relevance(_title: str, _desc: str) -> bool:
            return True

        monkeypatch.setattr(news_ingestion_service, "fetch_rss_feed", fake_fetch)
        monkeypatch.setattr(
            publisher_filters, "_fetch_silver_bulletin_archive", fake_archive
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "check_draft_relevance",
            fake_relevance,
        )
        monkeypatch.setattr(
            news_ingestion_service.news_summarization_service,
            "analyze_article",
            _stub_analysis,
        )

        added, _skipped, filtered, _mentions = await ingest_rss_source(
            db_session, _snapshot(source)
        )

        assert added == 1
        assert filtered == 0
        assert archive_calls == []  # publisher filter never invoked

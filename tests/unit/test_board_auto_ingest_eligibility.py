"""Unit tests for the pure eligibility predicate in board_auto_ingest_service.

No DB, no network.  All tests build minimal ``NewsItem``-like objects using
the real SQLModel class with only the fields relevant to the predicate.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.schemas.news_items import BoardExtractionResult, NewsItem, NewsItemTag
from app.services.board_auto_ingest_service import (
    _PAYWALL_RETRY_AFTER,
    _TRANSIENT_RETRY_AFTER,
    is_eligible,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 31, 12, 0, 0)
_CUTOFF = _NOW - timedelta(days=7)


def _item(
    *,
    tag: NewsItemTag = NewsItemTag.BIG_BOARD,
    published_at: datetime | None = None,
    last_extraction_attempted_at: datetime | None = None,
    last_extraction_result: BoardExtractionResult | None = None,
) -> NewsItem:
    """Build a minimal NewsItem for predicate tests (no DB required)."""
    return NewsItem(
        source_id=1,
        external_id="test-item",
        title="Test Article",
        url="https://example.com/p/test",
        tag=tag,
        published_at=published_at or _NOW - timedelta(days=1),
        last_extraction_attempted_at=last_extraction_attempted_at,
        last_extraction_result=last_extraction_result,
    )


# ---------------------------------------------------------------------------
# Tag filter
# ---------------------------------------------------------------------------


def test_big_board_tag_is_eligible() -> None:
    """BIG_BOARD-tagged items within the window are eligible by default."""
    item = _item(tag=NewsItemTag.BIG_BOARD)
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


def test_mock_draft_tag_is_eligible() -> None:
    """MOCK_DRAFT-tagged items within the window are eligible by default."""
    item = _item(tag=NewsItemTag.MOCK_DRAFT)
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


@pytest.mark.parametrize(
    "tag",
    [
        NewsItemTag.SCOUTING_REPORT,
        NewsItemTag.TIER_UPDATE,
        NewsItemTag.GAME_RECAP,
        NewsItemTag.FILM_STUDY,
        NewsItemTag.SKILL_THEME,
        NewsItemTag.TEAM_FIT,
        NewsItemTag.DRAFT_INTEL,
        NewsItemTag.STATS_ANALYSIS,
    ],
)
def test_non_board_tags_are_ineligible(tag: NewsItemTag) -> None:
    """Only BIG_BOARD and MOCK_DRAFT tags pass the tag filter."""
    item = _item(tag=tag)
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


# ---------------------------------------------------------------------------
# Date window filter
# ---------------------------------------------------------------------------


def test_item_within_window_is_eligible() -> None:
    """An item published 1 day ago (within 7-day window) is eligible."""
    item = _item(published_at=_NOW - timedelta(days=1))
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


def test_item_at_exact_cutoff_is_eligible() -> None:
    """An item published exactly at the cutoff is included (>= comparison)."""
    item = _item(published_at=_CUTOFF)
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


def test_item_before_window_is_ineligible() -> None:
    """An item published before the cutoff is excluded."""
    item = _item(published_at=_CUTOFF - timedelta(seconds=1))
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


# ---------------------------------------------------------------------------
# Extraction-memory: never attempted
# ---------------------------------------------------------------------------


def test_never_attempted_is_eligible() -> None:
    """Items with no extraction attempt recorded are always eligible."""
    item = _item(
        last_extraction_attempted_at=None,
        last_extraction_result=None,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


# ---------------------------------------------------------------------------
# Extraction-memory: permanent failures
# ---------------------------------------------------------------------------


def test_no_entries_is_never_retried() -> None:
    """NO_ENTRIES is a permanent outcome — never retry."""
    item = _item(
        last_extraction_attempted_at=_NOW - timedelta(hours=2),
        last_extraction_result=BoardExtractionResult.NO_ENTRIES,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


def test_unresolvable_is_never_retried() -> None:
    """UNRESOLVABLE is a permanent outcome — never retry."""
    item = _item(
        last_extraction_attempted_at=_NOW - timedelta(hours=2),
        last_extraction_result=BoardExtractionResult.UNRESOLVABLE,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


# ---------------------------------------------------------------------------
# Extraction-memory: TRANSIENT_ERROR retry
# ---------------------------------------------------------------------------


def test_transient_error_eligible_after_cooldown() -> None:
    """TRANSIENT_ERROR item is eligible once more than 1 hour has passed."""
    item = _item(
        last_extraction_attempted_at=_NOW - _TRANSIENT_RETRY_AFTER - timedelta(seconds=1),
        last_extraction_result=BoardExtractionResult.TRANSIENT_ERROR,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


def test_transient_error_not_eligible_within_cooldown() -> None:
    """TRANSIENT_ERROR item is ineligible if less than 1 hour has passed."""
    item = _item(
        last_extraction_attempted_at=_NOW - timedelta(minutes=30),
        last_extraction_result=BoardExtractionResult.TRANSIENT_ERROR,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


def test_transient_error_exactly_at_boundary_is_eligible() -> None:
    """TRANSIENT_ERROR item is eligible exactly at the 1-hour boundary."""
    item = _item(
        last_extraction_attempted_at=_NOW - _TRANSIENT_RETRY_AFTER,
        last_extraction_result=BoardExtractionResult.TRANSIENT_ERROR,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


# ---------------------------------------------------------------------------
# Extraction-memory: PAYWALLED retry
# ---------------------------------------------------------------------------


def test_paywalled_eligible_after_30_days() -> None:
    """PAYWALLED item is eligible once more than 30 days have passed."""
    item = _item(
        last_extraction_attempted_at=_NOW - _PAYWALL_RETRY_AFTER - timedelta(seconds=1),
        last_extraction_result=BoardExtractionResult.PAYWALLED,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


def test_paywalled_not_eligible_within_30_days() -> None:
    """PAYWALLED item is ineligible if less than 30 days have passed."""
    item = _item(
        last_extraction_attempted_at=_NOW - timedelta(days=15),
        last_extraction_result=BoardExtractionResult.PAYWALLED,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


def test_paywalled_exactly_at_boundary_is_eligible() -> None:
    """PAYWALLED item is eligible exactly at the 30-day boundary."""
    item = _item(
        last_extraction_attempted_at=_NOW - _PAYWALL_RETRY_AFTER,
        last_extraction_result=BoardExtractionResult.PAYWALLED,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


# ---------------------------------------------------------------------------
# Extraction-memory: SUCCESS
# ---------------------------------------------------------------------------


def test_success_is_eligible_for_dedup_check() -> None:
    """SUCCESS items pass the eligibility filter so extract_board's dedup runs."""
    item = _item(
        last_extraction_attempted_at=_NOW - timedelta(hours=1),
        last_extraction_result=BoardExtractionResult.SUCCESS,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is True


# ---------------------------------------------------------------------------
# Combined: tag + date + memory
# ---------------------------------------------------------------------------


def test_out_of_window_item_with_no_attempts_is_ineligible() -> None:
    """Date filter takes precedence — old items are excluded regardless of memory."""
    item = _item(
        tag=NewsItemTag.BIG_BOARD,
        published_at=_CUTOFF - timedelta(days=1),
        last_extraction_attempted_at=None,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False


def test_wrong_tag_within_window_is_ineligible() -> None:
    """Tag filter takes precedence over date and memory."""
    item = _item(
        tag=NewsItemTag.SCOUTING_REPORT,
        published_at=_NOW - timedelta(hours=1),
        last_extraction_attempted_at=None,
    )
    assert is_eligible(item, lookback_cutoff=_CUTOFF, now=_NOW) is False

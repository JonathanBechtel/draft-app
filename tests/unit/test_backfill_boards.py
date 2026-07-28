"""Transaction-boundary regressions for the operator board backfill."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.backfill_boards import _ingest_one


@pytest.mark.asyncio
async def test_new_news_item_commits_before_extraction_boundary() -> None:
    """A flushed source row is durable before extraction closes read state."""
    db = AsyncMock()
    candidate = {
        "source_id": 7,
        "post_date": "2026-06-01T12:00:00",
    }
    expected_boundary = db.close

    async def _extract(*args, **kwargs):
        assert db.commit.await_count == 1
        assert kwargs["before_network_io"] is expected_boundary
        return None

    with (
        patch(
            "scripts.backfill_boards._board_exists_for_date",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "scripts.backfill_boards._find_or_create_news_item",
            new=AsyncMock(return_value=SimpleNamespace(id=42)),
        ),
        patch(
            "app.services.board_extraction_service.extract_board",
            side_effect=_extract,
        ),
    ):
        status, _detail = await _ingest_one(db, candidate, kind="big_board")

    assert status == "empty"
    assert db.commit.await_count == 2

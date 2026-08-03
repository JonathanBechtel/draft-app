"""Unit tests for the pre-flip guards on Summer League metric publication."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.summer_league import metric_publish_guards as guards


def _grouped_scopes(
    rows: list[tuple[int, bool, bool]],
) -> MagicMock:
    """Return a session whose grouped scope query yields ``rows``."""
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
    return db


@pytest.mark.asyncio
async def test_assert_candidate_still_present_rejects_a_vanished_scope() -> None:
    """A scope holding current rows with no candidate row fails the publication."""
    db = _grouped_scopes([(3, True, False), (4, True, True)])

    with pytest.raises(guards.MetricCandidateVanishedError) as raised:
        await guards.assert_candidate_still_present(
            db,
            version=10,
            competition_ids=None,
            skipped_competition_ids=set(),
        )

    message = str(raised.value)
    assert "[3]" in message
    assert "summer_league_player_seasons" in message
    assert "10" in message


@pytest.mark.asyncio
async def test_assert_candidate_still_present_allows_a_first_publication() -> None:
    """Scopes with no current rows have nothing to lose and are not checked."""
    db = _grouped_scopes([(3, False, True), (4, False, False)])

    await guards.assert_candidate_still_present(
        db,
        version=10,
        competition_ids={3, 4},
        skipped_competition_ids=set(),
    )

    assert db.execute.await_count == len(guards.PROJECTION_MODELS)


@pytest.mark.asyncio
async def test_assert_candidate_still_present_ignores_scopes_left_untouched() -> None:
    """A scope skipped for a newer publication is never demoted, so never checked."""
    db = _grouped_scopes([(3, True, False)])

    await guards.assert_candidate_still_present(
        db,
        version=9,
        competition_ids=None,
        skipped_competition_ids={3},
    )

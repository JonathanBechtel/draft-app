"""Unit tests for shared Summer League scheduled-job lifecycle windows."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.schemas.summer_league import SummerLeagueCompetition
from app.services.summer_league import event_window


def _competition() -> SummerLeagueCompetition:
    """Build a configured competition window for lifecycle tests."""
    return SummerLeagueCompetition(
        year=2026,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2026 Las Vegas",
        starts_on=date(2026, 7, 9),
        ends_on=date(2026, 7, 19),
    )


def test_synthetic_schedule_dates_expands_inclusive_window() -> None:
    """Configured competition bounds become one date per calendar day."""
    assert event_window.synthetic_schedule_dates([_competition()]) == tuple(
        date(2026, 7, day) for day in range(9, 20)
    )


@pytest.mark.asyncio
async def test_window_guard_allows_active_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active configured event permits the caller to poll its source."""

    async def _resolve(_db: object, *, today: date) -> list[SummerLeagueCompetition]:
        return [_competition()]

    monkeypatch.setattr(event_window, "resolve_target_competitions", _resolve)

    assert await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_window_guard_rejects_archived_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished event outside the post-roll tail stays network-free."""

    async def _resolve(_db: object, *, today: date) -> list[SummerLeagueCompetition]:
        return [_competition()]

    monkeypatch.setattr(event_window, "resolve_target_competitions", _resolve)

    assert not await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),  # type: ignore[arg-type]
    )

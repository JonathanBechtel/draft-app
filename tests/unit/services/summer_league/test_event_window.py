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


@pytest.mark.asyncio
async def test_window_guard_scopes_requested_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requested roster year cannot inherit another season's window."""
    other_year = SummerLeagueCompetition(
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2025 Las Vegas",
        starts_on=date(2025, 7, 9),
        ends_on=date(2025, 7, 19),
    )

    async def _resolve(_db: object, *, today: date) -> list[SummerLeagueCompetition]:
        return [_competition(), other_year]

    monkeypatch.setattr(event_window, "resolve_target_competitions", _resolve)

    assert not await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),  # type: ignore[arg-type]
        year=2025,
    )


def _2027_competition() -> SummerLeagueCompetition:
    """Build a 2027 competition window, mirroring the 2026 fixture a year later."""
    return SummerLeagueCompetition(
        year=2027,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2027 Las Vegas",
        starts_on=date(2027, 7, 9),
        ends_on=date(2027, 7, 19),
    )


@pytest.mark.asyncio
async def test_year_scoped_to_finished_event_stays_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (a): the gate stays closed even when a year is scoped.

    SL_ROSTER_YEAR set to a year whose event already ended scopes -- but does
    not bypass -- the window, so the gate stays closed.
    """
    monkeypatch.setenv("SL_ROSTER_YEAR", "2027")

    async def _resolve(_db: object, *, today: date) -> list[SummerLeagueCompetition]:
        return [_2027_competition()]

    monkeypatch.setattr(event_window, "resolve_target_competitions", _resolve)

    assert not await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2027, 7, 25, tzinfo=timezone.utc),  # type: ignore[arg-type]
        year=2027,
    )


@pytest.mark.asyncio
async def test_no_year_set_opens_in_announced_window_next_season(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (b): no year override still opens next season on its own.

    With no year override, next season's competitions open the gate in the
    Announced window without any code change.
    """
    monkeypatch.delenv("SL_ROSTER_YEAR", raising=False)
    # A leaked SL_ROSTER_FORCE would satisfy the assertion below via the
    # bypass branch rather than the window logic actually under test.
    monkeypatch.delenv("SL_ROSTER_FORCE", raising=False)

    async def _resolve(_db: object, *, today: date) -> list[SummerLeagueCompetition]:
        return [_2027_competition()]

    monkeypatch.setattr(event_window, "resolve_target_competitions", _resolve)

    assert await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2027, 6, 27, tzinfo=timezone.utc),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_force_flag_bypasses_window_regardless_of_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario (c): the force flag bypasses the window regardless of year.

    SL_ROSTER_FORCE=1 permits an operator-directed backfill regardless of
    the resolved year or lifecycle phase.
    """
    monkeypatch.setenv("SL_ROSTER_FORCE", "1")

    async def _unexpected_resolve(
        _db: object, *, today: date
    ) -> list[SummerLeagueCompetition]:
        raise AssertionError("a force override should not resolve the active event")

    monkeypatch.setattr(
        event_window, "resolve_target_competitions", _unexpected_resolve
    )

    assert await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2027, 1, 1, tzinfo=timezone.utc),  # type: ignore[arg-type]
        year=2025,
    )


@pytest.mark.asyncio
async def test_year_alone_no_longer_bypasses_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting only SL_ROSTER_YEAR must not bypass the gate.

    This is the exact defect the ticket closes: SL_ROSTER_YEAR alone used to
    return True unconditionally.
    """
    monkeypatch.setenv("SL_ROSTER_YEAR", "2025")
    monkeypatch.delenv("SL_ROSTER_FORCE", raising=False)

    async def _resolve(_db: object, *, today: date) -> list[SummerLeagueCompetition]:
        return [_competition()]

    monkeypatch.setattr(event_window, "resolve_target_competitions", _resolve)

    assert not await event_window.is_summer_league_window_open(
        object(),
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),  # type: ignore[arg-type]
        year=2025,
    )


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", "OFF", "  ", ""])
def test_falsy_force_values_do_not_bypass_the_window(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """``SL_ROSTER_FORCE`` set to a falsy string must read as "not forced".

    Without this, emptying ``_FALSY_ENV_VALUES`` -- which would make
    ``SL_ROSTER_FORCE=0`` *enable* the bypass, the exact inversion an operator
    disabling the flag would hit -- passes the whole suite.
    """
    monkeypatch.setenv("SL_ROSTER_FORCE", raw)

    assert event_window.has_force_override() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", " 1 "])
def test_truthy_force_values_do_bypass_the_window(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """The positive control for the falsy set: an affirmative value forces the bypass."""
    monkeypatch.setenv("SL_ROSTER_FORCE", raw)

    assert event_window.has_force_override() is True


def test_unset_force_flag_is_not_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent ``SL_ROSTER_FORCE`` is the default, non-forcing state."""
    monkeypatch.delenv("SL_ROSTER_FORCE", raising=False)

    assert event_window.has_force_override() is False

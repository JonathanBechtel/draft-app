"""Unit coverage for ``_upsert_roster_team_entry``'s target resolution (#796).

Mirrors ``tests/unit/test_summer_league_normalization.py``'s coverage of
``_upsert_team_entry`` for the second team-entry creation site named in
ticket #796. No DB round-trip: a minimal ``AsyncSession`` stand-in returns a
canned SELECT result, and ``resolve_team_targets`` is monkeypatched directly
on the module.
"""

from __future__ import annotations

import pytest

from app.schemas.summer_league import SummerLeagueTeamEntry
from app.services.sources.summer_league import roster_ingest as service


class _FakeExecuteResult:
    def __init__(self, row: SummerLeagueTeamEntry | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> SummerLeagueTeamEntry | None:
        return self._row


class _FakeUpsertSession:
    """Minimal AsyncSession stand-in: one canned SELECT result, no writes."""

    def __init__(self, existing_row: SummerLeagueTeamEntry | None = None) -> None:
        self.existing_row = existing_row
        self.added: list[object] = []

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeExecuteResult:
        return _FakeExecuteResult(self.existing_row)

    def add(self, obj: object) -> None:
        self.added.append(obj)


@pytest.mark.asyncio
async def test_upsert_roster_team_entry_new_row_resolves_both_targets_on_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating a team entry resolves and stores both dual-read targets."""

    async def fake_resolve(
        _db: object, *, nba_stats_team_id: str
    ) -> tuple[int | None, int | None]:
        assert nba_stats_team_id == "1610612747"
        return (5, 900)

    monkeypatch.setattr(service, "resolve_team_targets", fake_resolve)
    db = _FakeUpsertSession(existing_row=None)

    row, created_unresolved = await service._upsert_roster_team_entry(
        db, 10, "1610612747"
    )

    assert row.nba_team_id == 5
    assert row.team_program_id == 900
    assert created_unresolved is False


@pytest.mark.asyncio
async def test_upsert_roster_team_entry_new_row_stays_null_when_resolution_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-NBA/select squad creates a row with both targets left NULL."""

    async def fake_resolve(
        _db: object, *, nba_stats_team_id: str
    ) -> tuple[int | None, int | None]:
        return (None, None)

    monkeypatch.setattr(service, "resolve_team_targets", fake_resolve)
    db = _FakeUpsertSession(existing_row=None)

    row, created_unresolved = await service._upsert_roster_team_entry(
        db, 10, "orlando-white"
    )

    assert row.nba_team_id is None
    assert row.team_program_id is None
    assert created_unresolved is True


@pytest.mark.asyncio
async def test_upsert_roster_team_entry_fills_null_target_without_overwriting_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing row with one target already set never has it overwritten."""
    existing = SummerLeagueTeamEntry(
        id=20,
        competition_id=10,
        nba_stats_team_id="1610612747",
        raw_team_name="1610612747",
        team_slug="1610612747",
        nba_team_id=None,
        team_program_id=77,
    )

    async def fake_resolve(
        _db: object, *, nba_stats_team_id: str
    ) -> tuple[int | None, int | None]:
        return (5, 999)

    monkeypatch.setattr(service, "resolve_team_targets", fake_resolve)
    db = _FakeUpsertSession(existing_row=existing)

    row, created_unresolved = await service._upsert_roster_team_entry(
        db, 10, "1610612747"
    )

    assert row.nba_team_id == 5  # filled -- was NULL
    assert row.team_program_id == 77  # untouched -- was already set
    assert created_unresolved is False


@pytest.mark.asyncio
async def test_upsert_roster_team_entry_skips_resolution_once_both_targets_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-resolved existing row never re-queries the resolver."""
    existing = SummerLeagueTeamEntry(
        id=20,
        competition_id=10,
        nba_stats_team_id="1610612747",
        raw_team_name="1610612747",
        team_slug="1610612747",
        nba_team_id=5,
        team_program_id=900,
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resolve_team_targets must not be called")

    monkeypatch.setattr(service, "resolve_team_targets", fail)
    db = _FakeUpsertSession(existing_row=existing)

    row, created_unresolved = await service._upsert_roster_team_entry(
        db, 10, "1610612747"
    )

    assert row.nba_team_id == 5
    assert row.team_program_id == 900
    assert created_unresolved is False

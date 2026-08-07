"""Unit coverage for ``_upsert_roster_team_entry``'s target resolution (#796)
and ``_announce_player``'s create-path target propagation (#794).

Mirrors ``tests/unit/test_summer_league_normalization.py``'s coverage of
``_upsert_team_entry`` for the second team-entry creation site named in
ticket #796. No DB round-trip: a minimal ``AsyncSession`` stand-in returns a
canned SELECT result, and ``resolve_team_targets`` is monkeypatched directly
on the module.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.schemas.player_affiliation import PlayerAffiliation
from app.schemas.summer_league import SummerLeagueSourceRecord, SummerLeagueTeamEntry
from app.services.sources.summer_league import roster_ingest as service
from app.services.sources.summer_league.roster_parse import RosterEntry


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


# ---------------------------------------------------------------------------
# _announce_player -- create-path target propagation (#794)
# ---------------------------------------------------------------------------


class _FakeAnnounceScalars:
    """Stand-in for the ``.scalars()`` half of an ``execute()`` result."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _FakeAnnounceResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeAnnounceScalars:
        return _FakeAnnounceScalars(self._rows)


class _FakeAnnounceSession:
    """Minimal AsyncSession stand-in for ``_announce_player``'s create path.

    ``execute`` always reports no existing participation (the "add" branch),
    so every call exercises the brand-new-affiliation code path this ticket
    changes. ``flush`` assigns a monotonically-increasing id to any added row
    that doesn't already have one, mirroring what a real flush does.
    """

    def __init__(self) -> None:
        self.added: list[object] = []
        self._next_id = 1

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeAnnounceResult:
        return _FakeAnnounceResult([])

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id  # type: ignore[attr-defined]
                self._next_id += 1


def _roster_entry(person_id: str = "P1", team_id: str = "1610612747") -> RosterEntry:
    return RosterEntry(
        nba_stats_person_id=person_id,
        raw_player_name=f"Player {person_id}",
        team_id=team_id,
        jersey="0",
        position="G",
        height="6-3",
        weight="185",
        birth_date=None,
        school=None,
        how_acquired=None,
        league_id="15",
    )


def _source_player(source_player_id: int = 1) -> SummerLeagueSourceRecord:
    return SummerLeagueSourceRecord(
        id=source_player_id,
        nba_stats_person_id="P1",
        raw_player_name="Player P1",
        normalized_name="player p1",
        canonical_player_id=None,
    )


@pytest.mark.asyncio
async def test_announce_player_new_affiliation_carries_team_entry_targets() -> None:
    """A brand-new ANNOUNCED affiliation copies its targets from the team entry.

    The create-path used to hardcode ``nba_team_id=None`` behind a
    "resolved later (T4 ticket)" comment and never passed ``team_program_id``
    at all. It must now read both targets from the already-resolved
    ``SummerLeagueTeamEntry`` in scope, per #796.
    """
    team_entry = SummerLeagueTeamEntry(
        id=20,
        competition_id=10,
        nba_stats_team_id="1610612747",
        raw_team_name="Los Angeles Lakers",
        team_slug="lakers",
        nba_team_id=5,
        team_program_id=900,
    )
    db = _FakeAnnounceSession()

    await service._announce_player(
        db,
        competition_id=10,
        team_entry=team_entry,
        source_player=_source_player(),
        entry=_roster_entry(),
        recorded_at=datetime(2026, 7, 1),
    )

    affiliations = [obj for obj in db.added if isinstance(obj, PlayerAffiliation)]
    assert len(affiliations) == 1
    assert affiliations[0].nba_team_id == 5
    assert affiliations[0].team_program_id == 900


@pytest.mark.asyncio
async def test_announce_player_unresolved_team_entry_yields_null_targets() -> None:
    """An unresolved team entry produces an affiliation with both targets NULL.

    Per this repo's entity-resolution rule, an unresolved team entry must
    never invent a target -- both stay ``None``, not a guess.
    """
    team_entry = SummerLeagueTeamEntry(
        id=21,
        competition_id=10,
        nba_stats_team_id="orlando-white",
        raw_team_name="orlando-white",
        team_slug="orlando-white",
        nba_team_id=None,
        team_program_id=None,
    )
    db = _FakeAnnounceSession()

    await service._announce_player(
        db,
        competition_id=10,
        team_entry=team_entry,
        source_player=_source_player(),
        entry=_roster_entry(team_id="orlando-white"),
        recorded_at=datetime(2026, 7, 1),
    )

    affiliations = [obj for obj in db.added if isinstance(obj, PlayerAffiliation)]
    assert len(affiliations) == 1
    assert affiliations[0].nba_team_id is None
    assert affiliations[0].team_program_id is None

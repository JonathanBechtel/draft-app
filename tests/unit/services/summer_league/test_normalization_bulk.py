"""Unit tests for the bulk shot/PBP upsert helpers in normalization.py (#627).

Exercises ``_bulk_upsert_source_players``, ``_bulk_upsert_shot_events``,
``_preload_actor_ids``, and ``_bulk_upsert_pbp_events`` against a fake
AsyncSession that records every ``execute``/``flush`` call, proving these
helpers issue one bulk statement per chunk -- never a SELECT or flush per
row -- for a multi-row batch. End-to-end row semantics (idempotency,
resolved/unresolved players, legacy-id crosswalk, ``game_ids`` batching) are
covered by the real-Postgres integration tests in
``tests/integration/services/test_shotchart_ingest.py`` and
``tests/integration/services/test_pbp_ingest.py``; this file isolates the
hot-path DB-call-count contract that motivated #627 (the 87.7-minute,
10,155-shot-event production incident caused by one SELECT + flush per
event).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.services.summer_league.normalization import (
    BULK_UPSERT_CHUNK_SIZE,
    ParsedPlayerGamelogRow,
    _bulk_upsert_pbp_events,
    _bulk_upsert_shot_events,
    _bulk_upsert_source_players,
    _preload_actor_ids,
)


@dataclass
class _FakeResult:
    """Stand-in for ``sqlalchemy.engine.Result`` -- only ``.all()`` is used here."""

    rows: list[tuple[Any, ...]]

    def all(self) -> list[tuple[Any, ...]]:
        """Return the canned rows, mirroring ``Result.all()``."""
        return self.rows


@dataclass
class _FakeSession:
    """Minimal AsyncSession stand-in that counts ``execute``/``flush`` calls.

    ``execute_results`` is consumed in call order -- one entry per expected
    ``db.execute`` invocation -- so a test can assert both the call count
    (proving no per-row round trips) and the values a helper builds from
    each result.
    """

    execute_results: list[_FakeResult] = field(default_factory=list)
    executed_statements: list[Any] = field(default_factory=list)
    execute_count: int = 0
    flush_count: int = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        """Record the call and return the next canned result (or empty)."""
        self.execute_count += 1
        self.executed_statements.append(stmt)
        if not self.execute_results:
            return _FakeResult(rows=[])
        return self.execute_results.pop(0)

    async def flush(self) -> None:
        """Record that a flush was requested."""
        self.flush_count += 1


def _shot_row(*, event_id: int, person_id: str = "1001") -> dict[str, Any]:
    return {
        "game_id": 1,
        "competition_id": 1,
        "team_entry_id": 1,
        "source_player_id": 11,
        "player_id": None,
        "nba_stats_person_id": person_id,
        "nba_stats_game_id": "G1",
        "nba_stats_game_event_id": event_id,
        "period": 1,
        "minutes_remaining": 9,
        "seconds_remaining": 30,
        "loc_x": 0,
        "loc_y": 0,
        "shot_distance": 24,
        "shot_type": "3PT Field Goal",
        "shot_zone_basic": "Above the Break 3",
        "shot_zone_area": "Center(C)",
        "shot_zone_range": "24+ ft.",
        "action_type": "Jump Shot",
        "made": True,
        "created_at": None,
        "updated_at": None,
    }


def _pbp_row(*, event_num: int) -> dict[str, Any]:
    return {
        "game_id": 1,
        "competition_id": 1,
        "nba_stats_game_id": "G1",
        "event_num": event_num,
        "period": 1,
        "clock": "5:30",
        "event_msg_type": 1,
        "home_score": 10,
        "away_score": 8,
        "score_margin": 2,
        "person1_nba_id": "2001",
        "person1_id": 500,
        "person2_nba_id": None,
        "person2_id": None,
        "person3_nba_id": None,
        "person3_id": None,
        "description": "Player A Dunk",
        "created_at": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_bulk_upsert_source_players_issues_one_statement_no_flush() -> None:
    """A multi-identity batch triggers exactly one execute(), zero flush()."""
    session = _FakeSession(
        execute_results=[
            _FakeResult(
                rows=[
                    ("1001", 11, None),
                    ("1002", 12, 900),
                    ("1003", 13, None),
                ]
            )
        ]
    )
    identities = {
        "1001": ParsedPlayerGamelogRow(
            nba_stats_person_id="1001", raw_player_name="A", nba_stats_team_id="1"
        ),
        "1002": ParsedPlayerGamelogRow(
            nba_stats_person_id="1002", raw_player_name="B", nba_stats_team_id="1"
        ),
        "1003": ParsedPlayerGamelogRow(
            nba_stats_person_id="1003", raw_player_name="C", nba_stats_team_id="2"
        ),
    }

    refs = await _bulk_upsert_source_players(session, identities, year=2024)  # type: ignore[arg-type]

    assert session.execute_count == 1
    assert session.flush_count == 0
    assert refs["1001"].id == 11
    assert refs["1001"].canonical_player_id is None
    assert refs["1002"].canonical_player_id == 900
    assert refs["1003"].id == 13


@pytest.mark.asyncio
async def test_bulk_upsert_source_players_empty_is_a_noop() -> None:
    """No identities means no DB round trip at all."""
    session = _FakeSession()

    refs = await _bulk_upsert_source_players(session, {}, year=2024)  # type: ignore[arg-type]

    assert refs == {}
    assert session.execute_count == 0
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_bulk_upsert_source_players_chunks_large_batches() -> None:
    """A batch larger than the chunk size issues one execute() per chunk, no flush()."""
    total = BULK_UPSERT_CHUNK_SIZE + 10
    identities = {
        str(i): ParsedPlayerGamelogRow(
            nba_stats_person_id=str(i), raw_player_name=f"P{i}", nba_stats_team_id="1"
        )
        for i in range(total)
    }
    session = _FakeSession(
        execute_results=[
            _FakeResult(
                rows=[(str(i), i, None) for i in range(BULK_UPSERT_CHUNK_SIZE)]
            ),
            _FakeResult(
                rows=[(str(i), i, None) for i in range(BULK_UPSERT_CHUNK_SIZE, total)]
            ),
        ]
    )

    refs = await _bulk_upsert_source_players(session, identities, year=2024)  # type: ignore[arg-type]

    assert session.execute_count == 2
    assert session.flush_count == 0
    assert len(refs) == total


@pytest.mark.asyncio
async def test_bulk_upsert_shot_events_issues_one_statement_no_flush() -> None:
    """Several shot rows write via exactly one chunked INSERT ... ON CONFLICT."""
    session = _FakeSession()
    rows = [_shot_row(event_id=i) for i in range(5)]

    await _bulk_upsert_shot_events(session, rows)  # type: ignore[arg-type]

    assert session.execute_count == 1
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_bulk_upsert_shot_events_empty_is_a_noop() -> None:
    """No rows means no DB round trip at all."""
    session = _FakeSession()

    await _bulk_upsert_shot_events(session, [])  # type: ignore[arg-type]

    assert session.execute_count == 0
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_bulk_upsert_shot_events_chunks_large_batches() -> None:
    """A batch larger than the chunk size issues one execute() per chunk, no flush()."""
    session = _FakeSession()
    total = BULK_UPSERT_CHUNK_SIZE + 10
    rows = [_shot_row(event_id=i) for i in range(total)]

    await _bulk_upsert_shot_events(session, rows)  # type: ignore[arg-type]

    assert session.execute_count == 2
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_preload_actor_ids_issues_one_statement_no_flush() -> None:
    """Several distinct actor ids resolve via exactly one SELECT ... IN (...)."""
    session = _FakeSession(
        execute_results=[_FakeResult(rows=[("2001", 500), ("2002", None)])]
    )

    actor_map = await _preload_actor_ids(session, {"2001", "2002", "2003"})  # type: ignore[arg-type]

    assert session.execute_count == 1
    assert session.flush_count == 0
    assert actor_map["2001"] == 500
    assert actor_map["2002"] is None
    # 2003 was never returned by the (fake) SELECT -- stays absent, same as
    # _resolve_actor_id's "no matching source player" -> None contract.
    assert "2003" not in actor_map


@pytest.mark.asyncio
async def test_preload_actor_ids_empty_is_a_noop() -> None:
    """An empty id set means no DB round trip at all."""
    session = _FakeSession()

    actor_map = await _preload_actor_ids(session, set())  # type: ignore[arg-type]

    assert actor_map == {}
    assert session.execute_count == 0
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_bulk_upsert_pbp_events_issues_one_statement_no_flush() -> None:
    """Several PBP rows write via exactly one chunked INSERT ... ON CONFLICT."""
    session = _FakeSession()
    rows = [_pbp_row(event_num=i) for i in range(5)]

    await _bulk_upsert_pbp_events(session, rows)  # type: ignore[arg-type]

    assert session.execute_count == 1
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_bulk_upsert_pbp_events_empty_is_a_noop() -> None:
    """No rows means no DB round trip at all."""
    session = _FakeSession()

    await _bulk_upsert_pbp_events(session, [])  # type: ignore[arg-type]

    assert session.execute_count == 0
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_bulk_upsert_pbp_events_chunks_large_batches() -> None:
    """A batch larger than the chunk size issues one execute() per chunk, no flush()."""
    session = _FakeSession()
    total = BULK_UPSERT_CHUNK_SIZE + 10
    rows = [_pbp_row(event_num=i) for i in range(total)]

    await _bulk_upsert_pbp_events(session, rows)  # type: ignore[arg-type]

    assert session.execute_count == 2
    assert session.flush_count == 0

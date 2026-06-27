"""Integration tests for SummerLeagueShotEvent and SummerLeaguePlayByPlayEvent tables.

Covers:
- Both tables are present after conftest setup.
- SummerLeagueShotEvent: unique constraint on (nba_stats_game_id, nba_stats_game_event_id)
  rejects duplicate insertion.
- SummerLeaguePlayByPlayEvent: unique constraint on (nba_stats_game_id, event_num)
  rejects duplicate insertion.
- Indexes (player_id, competition_id) and (game_id) exist on shot-event table.
- Index (game_id, period, event_num) exists on PBP-event table.
- player_id FK is nullable (unresolved shot stored with player_id=NULL).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayByPlayEvent,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_competition() -> SummerLeagueCompetition:
    return SummerLeagueCompetition(
        year=2025,
        league_id="15",
        venue_slug="vegas",
        display_name="Vegas Summer League 2025",
    )


def _make_game(competition_id: int) -> SummerLeagueGame:
    return SummerLeagueGame(
        competition_id=competition_id,
        nba_stats_game_id="0052500001",
        game_date=date(2025, 7, 12),
        status=SummerLeagueGameStatus.FINAL,
    )


def _make_team_entry(competition_id: int) -> SummerLeagueTeamEntry:
    return SummerLeagueTeamEntry(
        competition_id=competition_id,
        nba_stats_team_id="1610612744",
        raw_team_name="Golden State Warriors",
        team_slug="golden-state-warriors",
    )


def _make_source_player(competition_id: int, team_entry_id: int) -> SummerLeagueSourcePlayer:
    return SummerLeagueSourcePlayer(
        nba_stats_person_id="1629029",
        raw_player_name="Jordan Poole",
        normalized_name="jordan poole",
    )


def _make_shot_event(
    *,
    game_id: int,
    competition_id: int,
    team_entry_id: int,
    source_player_id: int,
    nba_stats_game_id: str = "0052500001",
    nba_stats_game_event_id: int = 1,
    player_id: int | None = None,
) -> SummerLeagueShotEvent:
    return SummerLeagueShotEvent(
        game_id=game_id,
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        player_id=player_id,
        nba_stats_person_id="1629029",
        nba_stats_game_id=nba_stats_game_id,
        nba_stats_game_event_id=nba_stats_game_event_id,
        period=1,
        minutes_remaining=8,
        seconds_remaining=30,
        loc_x=10,
        loc_y=5,
        shot_distance=3,
        shot_type="2PT Field Goal",
        shot_zone_basic="Restricted Area",
        shot_zone_area="Center(C)",
        shot_zone_range="Less Than 8 ft.",
        action_type="Layup Shot",
        made=True,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_pbp_event(
    *,
    game_id: int,
    competition_id: int,
    nba_stats_game_id: str = "0052500001",
    event_num: int = 1,
) -> SummerLeaguePlayByPlayEvent:
    return SummerLeaguePlayByPlayEvent(
        game_id=game_id,
        competition_id=competition_id,
        nba_stats_game_id=nba_stats_game_id,
        event_num=event_num,
        period=1,
        clock="PT08M30.00S",
        event_msg_type=1,
        home_score=2,
        away_score=0,
        score_margin=2,
        description="Layup Shot: Jordan Poole (2 PTS)",
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _setup_base_rows(
    db_session: AsyncSession,
) -> tuple[int, int, int, int]:
    """Create competition, game, team entry, and source player; return their IDs."""
    comp = _make_competition()
    db_session.add(comp)
    await db_session.flush()

    game = _make_game(comp.id)  # type: ignore[arg-type]
    db_session.add(game)
    await db_session.flush()

    entry = _make_team_entry(comp.id)  # type: ignore[arg-type]
    db_session.add(entry)
    await db_session.flush()

    source_player = _make_source_player(comp.id, entry.id)  # type: ignore[arg-type]
    db_session.add(source_player)
    await db_session.flush()

    return (
        comp.id,  # type: ignore[return-value]
        game.id,  # type: ignore[return-value]
        entry.id,  # type: ignore[return-value]
        source_player.id,  # type: ignore[return-value]
    )


# ---------------------------------------------------------------------------
# Table-presence tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shot_event_table_exists(db_session: AsyncSession, test_schema: str) -> None:
    """summer_league_shot_events table is present in the test schema after setup."""
    result = await db_session.execute(
        text(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = :schema AND tablename = 'summer_league_shot_events'"
        ),
        {"schema": test_schema},
    )
    assert result.fetchone() is not None, "summer_league_shot_events table not found"


@pytest.mark.asyncio
async def test_pbp_event_table_exists(db_session: AsyncSession, test_schema: str) -> None:
    """summer_league_play_by_play_events table is present in the test schema after setup."""
    result = await db_session.execute(
        text(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = :schema AND tablename = 'summer_league_play_by_play_events'"
        ),
        {"schema": test_schema},
    )
    assert result.fetchone() is not None, "summer_league_play_by_play_events table not found"


# ---------------------------------------------------------------------------
# Shot event: basic insert + nullable player_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shot_event_insert_round_trip(db_session: AsyncSession) -> None:
    """A SummerLeagueShotEvent inserts and round-trips cleanly via SELECT."""
    comp_id, game_id, entry_id, sp_id = await _setup_base_rows(db_session)

    event = _make_shot_event(
        game_id=game_id,
        competition_id=comp_id,
        team_entry_id=entry_id,
        source_player_id=sp_id,
    )
    db_session.add(event)
    await db_session.flush()

    assert event.id is not None
    result = await db_session.execute(
        text(
            "SELECT made, shot_zone_basic, player_id"
            " FROM summer_league_shot_events WHERE id = :id"
        ),
        {"id": event.id},
    )
    row = result.one()
    assert row.made is True
    assert row.shot_zone_basic == "Restricted Area"
    assert row.player_id is None


@pytest.mark.asyncio
async def test_shot_event_null_player_id_stored(db_session: AsyncSession) -> None:
    """Unresolved shot (player_id=NULL) is stored without error."""
    comp_id, game_id, entry_id, sp_id = await _setup_base_rows(db_session)

    event = _make_shot_event(
        game_id=game_id,
        competition_id=comp_id,
        team_entry_id=entry_id,
        source_player_id=sp_id,
        player_id=None,
    )
    db_session.add(event)
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT player_id FROM summer_league_shot_events WHERE id = :id"),
        {"id": event.id},
    )
    assert result.scalar_one() is None


# ---------------------------------------------------------------------------
# Shot event: unique constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shot_event_unique_constraint_rejects_duplicate(
    db_session: AsyncSession,
) -> None:
    """Duplicate (nba_stats_game_id, nba_stats_game_event_id) is rejected."""
    comp_id, game_id, entry_id, sp_id = await _setup_base_rows(db_session)

    event1 = _make_shot_event(
        game_id=game_id,
        competition_id=comp_id,
        team_entry_id=entry_id,
        source_player_id=sp_id,
        nba_stats_game_id="0052500001",
        nba_stats_game_event_id=42,
    )
    event2 = _make_shot_event(
        game_id=game_id,
        competition_id=comp_id,
        team_entry_id=entry_id,
        source_player_id=sp_id,
        nba_stats_game_id="0052500001",
        nba_stats_game_event_id=42,
    )
    db_session.add(event1)
    await db_session.flush()

    db_session.add(event2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Shot event: indexes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shot_event_indexes_present(
    db_session: AsyncSession, test_schema: str
) -> None:
    """The (player_id, competition_id) and (game_id) indexes exist on shot events."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = :schema AND tablename = 'summer_league_shot_events'"
        ),
        {"schema": test_schema},
    )
    index_names = {row.indexname for row in result.fetchall()}

    assert "ix_summer_league_shot_events_player_competition" in index_names, (
        f"Missing player_competition index; found: {index_names}"
    )
    assert "ix_summer_league_shot_events_game_id" in index_names, (
        f"Missing game_id index; found: {index_names}"
    )


# ---------------------------------------------------------------------------
# PBP event: basic insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbp_event_insert_round_trip(db_session: AsyncSession) -> None:
    """A SummerLeaguePlayByPlayEvent inserts and round-trips cleanly via SELECT."""
    comp_id, game_id, _entry_id, _sp_id = await _setup_base_rows(db_session)

    event = _make_pbp_event(game_id=game_id, competition_id=comp_id)
    db_session.add(event)
    await db_session.flush()

    assert event.id is not None
    result = await db_session.execute(
        text(
            "SELECT period, event_num, home_score, away_score, description"
            " FROM summer_league_play_by_play_events WHERE id = :id"
        ),
        {"id": event.id},
    )
    row = result.one()
    assert row.period == 1
    assert row.event_num == 1
    assert row.home_score == 2
    assert row.away_score == 0
    assert "Jordan Poole" in row.description


# ---------------------------------------------------------------------------
# PBP event: unique constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbp_event_unique_constraint_rejects_duplicate(
    db_session: AsyncSession,
) -> None:
    """Duplicate (nba_stats_game_id, event_num) is rejected."""
    comp_id, game_id, _entry_id, _sp_id = await _setup_base_rows(db_session)

    event1 = _make_pbp_event(
        game_id=game_id,
        competition_id=comp_id,
        nba_stats_game_id="0052500001",
        event_num=99,
    )
    event2 = _make_pbp_event(
        game_id=game_id,
        competition_id=comp_id,
        nba_stats_game_id="0052500001",
        event_num=99,
    )
    db_session.add(event1)
    await db_session.flush()

    db_session.add(event2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# PBP event: index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbp_event_index_present(
    db_session: AsyncSession, test_schema: str
) -> None:
    """The (game_id, period, event_num) index exists on PBP events."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = :schema"
            " AND tablename = 'summer_league_play_by_play_events'"
        ),
        {"schema": test_schema},
    )
    index_names = {row.indexname for row in result.fetchall()}

    assert "ix_summer_league_pbp_events_game_period_event" in index_names, (
        f"Missing game_period_event index; found: {index_names}"
    )

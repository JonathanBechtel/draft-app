"""Integration tests for the EventDesk controller (`run_event_desk_tick`).

Exercises the full tick against real Summer League schedule data: the controller
must register the SL `events` row, resolve calendar facts from
`summer_league_games`, compute lifecycle phase + inner daily state, and upsert
`event_desk_state` -- with SL as the single (unopposed) home owner while Active.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import Event, EventDailyState, EventDeskState, EventLifecyclePhase
from app.schemas.summer_league import SummerLeagueCompetition, SummerLeagueGame, SummerLeagueGameStatus
from app.services.event_desk.controller import run_event_desk_tick
from app.services.summer_league.scoreboard_ingest import EVENT_KEY_SUMMER_LEAGUE

_YEAR = 2026


async def _make_competition(db: AsyncSession, *, venue_slug: str = "vegas") -> SummerLeagueCompetition:
    competition = SummerLeagueCompetition(
        year=_YEAR,
        league_id="10",
        venue_slug=venue_slug,
        display_name="2026 Las Vegas Summer League",
    )
    db.add(competition)
    await db.flush()
    await db.refresh(competition)
    return competition


async def _make_game(
    db: AsyncSession,
    *,
    competition_id: int,
    game_date: date,
    tip_datetime: datetime | None,
    status: SummerLeagueGameStatus,
    suffix: str,
) -> SummerLeagueGame:
    game = SummerLeagueGame(
        competition_id=competition_id,
        nba_stats_game_id=f"002026{suffix}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        status=status,
    )
    db.add(game)
    await db.flush()
    return game


@pytest.mark.asyncio
async def test_tick_registers_sl_event_row(db_session: AsyncSession) -> None:
    """The tick creates/refreshes the `events` row for Summer League with the
    resolved competition_ids and the pinned V1 priors/priority."""
    competition = await _make_competition(db_session)
    assert competition.id is not None
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 19, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        suffix="0001",
    )

    now = datetime(2026, 7, 10, 20, 0)
    await run_event_desk_tick(db_session, now=now)

    result = await db_session.execute(
        select(Event).where(Event.key == EVENT_KEY_SUMMER_LEAGUE)  # type: ignore[arg-type]
    )
    event_row = result.scalar_one()
    assert event_row.priority == 100
    assert event_row.is_active is True
    assert event_row.calendar_ref["competition_ids"] == [competition.id]
    assert event_row.window_priors["gap_bridge_days"] == 4


@pytest.mark.asyncio
async def test_tick_upserts_active_live_state_and_sl_owns_home(
    db_session: AsyncSession,
) -> None:
    """With a bridged multi-day window and a game in progress today, the controller
    resolves lifecycle=active, daily_state=live, and SL (unopposed) owns home."""
    competition = await _make_competition(db_session)
    assert competition.id is not None
    # Gap-bridged cluster: Jul 5 -> Jul 8 -> Jul 10 (gaps of 3 and 2 days, both
    # <= the default gap_bridge_days=4), so Jul 10 sits inside one Active window.
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 5),
        tip_datetime=datetime(2026, 7, 5, 19, 0),
        status=SummerLeagueGameStatus.FINAL,
        suffix="0001",
    )
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 8),
        tip_datetime=datetime(2026, 7, 8, 19, 0),
        status=SummerLeagueGameStatus.FINAL,
        suffix="0002",
    )
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 19, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        suffix="0003",
    )

    now = datetime(2026, 7, 10, 20, 0)
    states = await run_event_desk_tick(db_session, now=now)

    assert len(states) == 1
    state = states[0]
    assert state.lifecycle_phase == EventLifecyclePhase.ACTIVE
    assert state.daily_state == EventDailyState.LIVE
    assert state.is_home_owner is True
    assert state.freshness_tick_at == now
    assert state.next_tick_eta == datetime(2026, 7, 10, 21, 0)

    # Persisted row matches the returned DTO (single row per event, per the unique
    # constraint on event_id).
    result = await db_session.execute(
        select(EventDeskState).where(EventDeskState.event_id == state.event_id)  # type: ignore[arg-type]
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].is_home_owner is True


@pytest.mark.asyncio
async def test_tick_is_idempotent_and_refreshes_existing_state_row(
    db_session: AsyncSession,
) -> None:
    """A second tick updates the same `event_desk_state` row rather than inserting
    a duplicate (the `uq_event_desk_state_event` constraint's whole point)."""
    competition = await _make_competition(db_session)
    assert competition.id is not None
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 19, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0001",
    )

    first_tick_at = datetime(2026, 7, 10, 12, 0)  # before the flip -> Recap persists
    await run_event_desk_tick(db_session, now=first_tick_at)

    second_tick_at = datetime(2026, 7, 10, 19, 5)  # past tip, stale status -> Live
    states = await run_event_desk_tick(db_session, now=second_tick_at)

    assert len(states) == 1
    assert states[0].daily_state == EventDailyState.LIVE
    assert states[0].freshness_tick_at == second_tick_at

    result = await db_session.execute(select(EventDeskState))
    rows = result.scalars().all()
    assert len(rows) == 1  # updated in place, not duplicated


@pytest.mark.asyncio
async def test_tick_off_window_yields_no_home_owner_and_null_daily_state(
    db_session: AsyncSession,
) -> None:
    """Far outside the SL calendar window, the lifecycle phase is Dormant, the
    daily_state stays null, and SL doesn't own the home page."""
    competition = await _make_competition(db_session)
    assert competition.id is not None
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 19, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0001",
    )

    now = datetime(2026, 1, 1, 12, 0)
    states = await run_event_desk_tick(db_session, now=now)

    assert len(states) == 1
    assert states[0].lifecycle_phase == EventLifecyclePhase.DORMANT
    assert states[0].daily_state is None
    assert states[0].is_home_owner is False

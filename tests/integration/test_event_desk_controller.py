"""Integration tests for the EventDesk controller (`run_event_desk_tick`).

Exercises the full tick against real Summer League schedule data: the controller
must register the SL `events` row, resolve calendar facts from
`summer_league_games`, compute lifecycle phase + inner daily state, and upsert
`event_desk_state` -- with SL as the single (unopposed) home owner while Active.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import (
    Event,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
)
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
)
from app.services.event_desk.controller import run_event_desk_tick
from app.services.event_desk.registry import (
    GameStatus,
    calendar_facts_for_competition_ids,
)
from app.services.summer_league.scoreboard_ingest import EVENT_KEY_SUMMER_LEAGUE

_YEAR = 2026


async def _make_competition(
    db: AsyncSession, *, venue_slug: str = "vegas"
) -> SummerLeagueEdition:
    competition = SummerLeagueEdition(
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
    status_text: str | None = None,
) -> SummerLeagueGame:
    game = SummerLeagueGame(
        competition_id=competition_id,
        nba_stats_game_id=f"002026{suffix}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        status=status,
        status_text=status_text,
    )
    db.add(game)
    await db.flush()
    return game


@pytest.mark.asyncio
async def test_tick_registers_sl_event_row(db_session: AsyncSession) -> None:
    """Create or refresh the Summer League event row.

    The row carries resolved competition IDs and the pinned V1 priors/priority.
    """
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
    await run_event_desk_tick(db_session, now=now, content_updated=True)

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
    """Resolve an active live state owned by Summer League.

    The calendar uses a bridged multi-day window with a game in progress today.
    """
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
    states = await run_event_desk_tick(db_session, now=now, content_updated=True)

    assert len(states) == 1
    state = states[0]
    assert state.lifecycle_phase == EventLifecyclePhase.ACTIVE
    assert state.daily_state == EventDailyState.LIVE
    assert state.is_home_owner is True
    assert state.content_refreshed_at == now
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
    """Update the same state row on a second tick.

    The unique event constraint prevents a duplicate state row.
    """
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
    await run_event_desk_tick(db_session, now=first_tick_at, content_updated=True)

    second_tick_at = datetime(2026, 7, 10, 19, 5)  # past tip, stale status -> Live
    states = await run_event_desk_tick(
        db_session, now=second_tick_at, content_updated=True
    )

    assert len(states) == 1
    assert states[0].daily_state == EventDailyState.LIVE
    assert states[0].content_refreshed_at == second_tick_at


@pytest.mark.asyncio
async def test_lifecycle_only_tick_preserves_content_watermark(
    db_session: AsyncSession,
) -> None:
    """Lifecycle observation advances independently from content freshness."""
    competition = await _make_competition(db_session)
    assert competition.id is not None
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 19, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0099",
    )
    refreshed_at = datetime(2026, 7, 10, 18, 0)
    observed_at = datetime(2026, 7, 10, 19, 0)
    await run_event_desk_tick(db_session, now=refreshed_at, content_updated=True)

    states = await run_event_desk_tick(
        db_session, now=observed_at, content_updated=False
    )

    assert states[0].lifecycle_observed_at == observed_at
    assert states[0].content_refreshed_at == refreshed_at
    assert states[0].next_tick_eta == refreshed_at + timedelta(hours=1)


@pytest.mark.asyncio
async def test_lifecycle_only_first_tick_has_null_content_watermark(
    db_session: AsyncSession,
) -> None:
    """Represent observed lifecycle state without inventing a content refresh."""
    await _make_competition(db_session)

    states = await run_event_desk_tick(
        db_session,
        now=datetime(2026, 1, 1, 12, 0),
        content_updated=False,
    )

    assert states[0].lifecycle_phase == EventLifecyclePhase.DORMANT
    assert states[0].content_refreshed_at is None
    assert states[0].next_tick_eta is None

    result = await db_session.execute(select(EventDeskState))
    rows = result.scalars().all()
    assert len(rows) == 1  # updated in place, not duplicated


@pytest.mark.asyncio
async def test_tick_off_window_yields_no_home_owner_and_null_daily_state(
    db_session: AsyncSession,
) -> None:
    """Resolve Dormant far outside the Summer League window.

    The daily state stays null and Summer League does not own the home page.
    """
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
    states = await run_event_desk_tick(db_session, now=now, content_updated=True)

    assert len(states) == 1
    assert states[0].lifecycle_phase == EventLifecyclePhase.DORMANT
    assert states[0].daily_state is None
    assert states[0].is_home_owner is False


@pytest.mark.asyncio
async def test_calendar_facts_excludes_postponed_tip_but_surfaces_postponed_status(
    db_session: AsyncSession,
) -> None:
    """Re-detect a postponed game from persisted status text.

    `calendar_facts_for_competition_ids` reads the marker from
    ``status_text`` (persisted as `SCHEDULED` on the DB status column -- #529) and
    withholds its tip from `today_schedule` while still surfacing the terminal
    `GameStatus.POSTPONED` in `today_statuses` -- the provider-layer half of the
    #529/#530 follow-up fix.
    """
    competition = await _make_competition(db_session)
    assert competition.id is not None
    real_tip = datetime(2026, 7, 10, 23, 0)
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=real_tip,
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0001",
    )
    postponed_tip = datetime(2026, 7, 10, 10, 0)
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=postponed_tip,
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0002",
        status_text="PPD",
    )

    facts = await calendar_facts_for_competition_ids(
        db_session, competition_ids=[competition.id], today=date(2026, 7, 10)
    )

    # Only the real game's tip drives `first_tip` math -- the postponed game's
    # (earlier) tip is withheld entirely.
    assert facts.today_schedule == (real_tip,)
    assert sorted(facts.today_statuses) == sorted(
        (GameStatus.SCHEDULED, GameStatus.POSTPONED)
    )


@pytest.mark.asyncio
async def test_calendar_facts_maps_db_postponed_and_canceled_status_directly(
    db_session: AsyncSession,
) -> None:
    """Map persisted POSTPONED and CANCELED statuses directly.

    Fix #4 ensures a row with the real database status maps to
    `GameStatus.POSTPONED` with no ``status_text`` marker needed -- the direct
    status-level reconciliation, as distinct from the pre-fix-#4 ``status_text``
    fallback exercised by `test_calendar_facts_excludes_postponed_tip_but_surfaces_postponed_status`
    above (which still persists SCHEDULED+"PPD", proving that legacy path stays green).
    """
    competition = await _make_competition(db_session)
    assert competition.id is not None
    real_tip = datetime(2026, 7, 10, 23, 0)
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=real_tip,
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0001",
    )
    postponed_tip = datetime(2026, 7, 10, 10, 0)
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=postponed_tip,
        status=SummerLeagueGameStatus.POSTPONED,
        suffix="0002",
        status_text="PPD",
    )
    canceled_tip = datetime(2026, 7, 10, 11, 0)
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 10),
        tip_datetime=canceled_tip,
        status=SummerLeagueGameStatus.CANCELED,
        suffix="0003",
        status_text="Canceled",
    )

    facts = await calendar_facts_for_competition_ids(
        db_session, competition_ids=[competition.id], today=date(2026, 7, 10)
    )

    # Only the real game's tip drives `first_tip` math -- both the postponed and
    # canceled games' tips are withheld entirely.
    assert facts.today_schedule == (real_tip,)
    # Both POSTPONED and CANCELED collapse to the single terminal generic bucket.
    assert sorted(facts.today_statuses) == sorted(
        (GameStatus.SCHEDULED, GameStatus.POSTPONED, GameStatus.POSTPONED)
    )


@pytest.mark.asyncio
async def test_tick_postponed_only_day_resolves_recap_not_live(
    db_session: AsyncSession,
) -> None:
    """Resolve Recap when the day's only game is postponed.

    Core regression (#529/#530 follow-up): a day whose only game is postponed,
    with its original tip already in the past, must resolve to Recap -- never get
    stuck in Live forever off a game that will never tip.
    """
    competition = await _make_competition(db_session)
    assert competition.id is not None
    # Gap-bridged cluster so Jul 10 sits inside one Active window (same shape as
    # `test_tick_upserts_active_live_state_and_sl_owns_home` above).
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
        # Original tip is well in the past relative to `now` below -- pre-fix, this
        # was the exact condition that stranded the day in Live forever.
        tip_datetime=datetime(2026, 7, 10, 19, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0003",
        status_text="PPD",
    )

    now = datetime(2026, 7, 10, 23, 0)  # hours past the postponed game's original tip
    states = await run_event_desk_tick(db_session, now=now, content_updated=True)

    assert len(states) == 1
    assert states[0].lifecycle_phase == EventLifecyclePhase.ACTIVE
    assert states[0].daily_state == EventDailyState.RECAP


@pytest.mark.asyncio
async def test_tick_mixed_day_preview_then_live_then_recap_around_postponed_game(
    db_session: AsyncSession,
) -> None:
    """Ignore a postponed tip when resolving a mixed day.

    A mixed day -- one real game plus one postponed game with an earlier past
    tip -- must not flip Live off the postponed game's tip. It stays Preview until
    the real game's own tip, goes Live there, then reaches Recap once the real game
    finals (the postponed game staying postponed forever).
    """
    competition = await _make_competition(db_session)
    assert competition.id is not None
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 5),
        tip_datetime=datetime(2026, 7, 5, 19, 0),
        status=SummerLeagueGameStatus.FINAL,
        suffix="0001",
    )
    real_tip = datetime(2026, 7, 8, 23, 0)  # 19:00 EDT
    real_game = await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 8),
        tip_datetime=real_tip,
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0002",
    )
    await _make_game(
        db_session,
        competition_id=competition.id,
        game_date=date(2026, 7, 8),
        # Earlier same-day tip -- pre-fix, `first_tip = min(schedule)` would have
        # picked this one and flipped the whole day Live hours before the real tip.
        tip_datetime=datetime(2026, 7, 8, 10, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
        suffix="0003",
        status_text="PPD",
    )

    # After the flip (real tip - 6h lead = 17:00 UTC) but hours before the real tip
    # and well after the postponed game's own past tip -- must stay Preview, not
    # jump to Live off the postponed game.
    still_preview_at = datetime(2026, 7, 8, 18, 0)
    states = await run_event_desk_tick(
        db_session, now=still_preview_at, content_updated=True
    )
    assert states[0].daily_state == EventDailyState.PREVIEW

    # `_upsert_event_desk_state` writes via a raw Core `INSERT ... ON CONFLICT`, which
    # bypasses the ORM's unit-of-work: the `EventDeskState` instance from the prior
    # tick stays resident (and unmodified-looking) in the session's identity map, so
    # a later `select()` for the same row returns that same stale Python object
    # rather than the just-written values (same repo-established gotcha handled in
    # `test_sl_desk_render_snapshot_materialization.py` / `college_stats_service.py`'s
    # own `db.expire_all()`). Multiple ticks per test, on the *same* session, need an
    # explicit `expire_all()` between them so each tick's returned/queried row reads
    # its own fresh write rather than a resident stale one.
    db_session.expire_all()

    # At the real game's own tip -- Live.
    at_real_tip = real_tip
    states = await run_event_desk_tick(
        db_session, now=at_real_tip, content_updated=True
    )
    assert states[0].daily_state == EventDailyState.LIVE

    db_session.expire_all()

    # The real game finals; the postponed game stays postponed -- Recap, not stuck
    # Live off the still-unresolved-looking postponed game.
    real_game.status = SummerLeagueGameStatus.FINAL
    await db_session.flush()
    after_final = datetime(2026, 7, 9, 1, 0)
    states = await run_event_desk_tick(
        db_session, now=after_final, content_updated=True
    )
    assert states[0].daily_state == EventDailyState.RECAP

"""Per-state query-count guard for the Desk's request-time state resolution (#548).

`app.services.summer_league.desk_read.get_desk_view_from_snapshot` -- the ONLY
function `app.routes.ui.home` calls for the Summer League Desk (#551) -- does
exactly two things per request: resolve the current window/daily state fresh
(`_resolve_window_state`) and, if in-window, read ONE persisted render
snapshot row. Ticket #548's launch-readiness contract is that this "Desk
query" cost -- independent of whatever else `/` renders (consensus hero,
trending, news, podcasts, film room) -- stays at or below **five** queries
for every state: Off-window, Preview, Live, Recap, and Wind-down.

Unlike `tests/integration/perf/test_desk_home_inwindow_budget.py` (which
measures the WHOLE `/` HTTP route, seeded with a full tick's worth of
grades/storylines/commentary so the page has real content to render), these
tests call `get_desk_view_from_snapshot` directly and seed only what
`_resolve_window_state` actually reads (an `events` row + `summer_league_games`
rows) -- state/phase resolution never touches players, T1-T4, or any render
snapshot content, so a missing/empty snapshot doesn't change the query count
(`get_render_snapshot` is one query either way, hit or miss). This keeps the
suite fast and isolates exactly the query cost this ticket's budget is about.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueTeamEntry,
)
from app.services.event_desk.registry import sync_summer_league_event
from app.services.summer_league.desk_read import get_desk_view_from_snapshot
from tests.integration.perf._capture import count_queries

pytestmark = pytest.mark.asyncio

# Every Desk state this ticket's ≤5-query contract covers (behavioral DoD:
# "Off-window/Preview/Live/Recap/Wind-down remain at or below five Desk
# queries").
_DESK_QUERY_BUDGET = 5

_NOW = datetime(2026, 7, 15, 19, 0)  # ~3pm ET (EDT, UTC-4) on 2026-07-15.
_TODAY = date(2026, 7, 15)

_IDX = {"n": 0}


def _idx() -> int:
    _IDX["n"] += 1
    return _IDX["n"]


async def _seed_competition(
    db: AsyncSession, *, starts_on: date, ends_on: date, year: int = 2026
) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug=f"state-budget-{_idx()}",
        display_name="State Resolution Budget Fixture",
        starts_on=starts_on,
        ends_on=ends_on,
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueCompetition
) -> SummerLeagueTeamEntry:
    i = _idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"state-budget-team-{i}",
        raw_team_name=f"Team {i}",
        team_slug=f"state-budget-team-{i}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _seed_game(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    *,
    game_date: date,
    tip_datetime: datetime,
    status: SummerLeagueGameStatus,
) -> SummerLeagueGame:
    assert competition.id is not None and home.id is not None and away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"state-budget-game-{_idx()}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=status,
    )
    db.add(game)
    await db.flush()
    return game


async def _assert_budget(
    db: AsyncSession, async_engine: AsyncEngine, *, label: str
) -> None:
    with count_queries(async_engine) as captured:
        await get_desk_view_from_snapshot(db, now=_NOW)
    assert len(captured) <= _DESK_QUERY_BUDGET, (
        f"get_desk_view_from_snapshot ({label}) issued {len(captured)} queries, "
        f"over the #548 Desk-query budget of {_DESK_QUERY_BUDGET}.\n"
        + "\n".join(f"  {i + 1:>2}. {' '.join(s.split())[:120]}" for i, s in enumerate(captured))
    )


async def test_off_window_no_event_row_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """No `events` row at all (the year-round default before any tick has run).

    Cheapest path by design: one `events` lookup finds nothing and
    short-circuits -- 1 query, well under budget.
    """
    await _assert_budget(db_session, async_engine, label="off_window (no event row)")


async def test_off_window_dormant_with_event_row_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """A synced `events` row exists but the event has zero games -- genuinely Dormant.

    This is the real year-round production shape once the hourly cron has run
    at least once (it upserts the `events` row every tick, in-window or not) --
    `ROUTE_BUDGETS["/"]`'s off-window measurement only ever exercises the
    no-row case (its seeded dataset has no `events` row), so this proves the
    other honest off-window shape separately.
    """
    competition = await _seed_competition(
        db_session, starts_on=date(2026, 1, 1), ends_on=date(2026, 1, 10)
    )
    await sync_summer_league_event(db_session, _TODAY)
    await db_session.commit()
    assert competition.id is not None

    await _assert_budget(
        db_session, async_engine, label="off_window (dormant, event row exists)"
    )


async def test_preview_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """A scheduled, not-yet-tipped game today, past the Morning flip -> Preview."""
    competition = await _seed_competition(
        db_session, starts_on=date(2026, 7, 13), ends_on=date(2026, 7, 17)
    )
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=_TODAY,
        tip_datetime=_NOW + timedelta(hours=2),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    await sync_summer_league_event(db_session, _TODAY)
    await db_session.commit()

    await _assert_budget(db_session, async_engine, label="preview")


async def test_live_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """An in-progress game today -> Live (Live always wins)."""
    competition = await _seed_competition(
        db_session, starts_on=date(2026, 7, 13), ends_on=date(2026, 7, 17)
    )
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=_TODAY,
        tip_datetime=_NOW - timedelta(hours=1),
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    await sync_summer_league_event(db_session, _TODAY)
    await db_session.commit()

    await _assert_budget(db_session, async_engine, label="live")


async def test_recap_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Every game today is final -> Recap (the Ledger persists into the evening)."""
    competition = await _seed_competition(
        db_session, starts_on=date(2026, 7, 13), ends_on=date(2026, 7, 17)
    )
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=_TODAY,
        tip_datetime=_NOW - timedelta(hours=3),
        status=SummerLeagueGameStatus.FINAL,
    )
    await sync_summer_league_event(db_session, _TODAY)
    await db_session.commit()

    await _assert_budget(db_session, async_engine, label="recap")


async def test_wind_down_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """The window closed yesterday, still inside `post_roll_days` -> Wind-down.

    `desk_read._resolve_window_state` sets `daily_state=RECAP` directly for
    Wind-down (skipping `inner_state` entirely -- see that function's
    docstring), so this exercises the SAME downstream query path as Recap
    above; asserted separately since it's one of the DoD's five named states.
    """
    competition = await _seed_competition(
        db_session, starts_on=date(2026, 7, 12), ends_on=date(2026, 7, 14)
    )
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 14),
        tip_datetime=datetime(2026, 7, 14, 23, 0),
        status=SummerLeagueGameStatus.FINAL,
    )
    await sync_summer_league_event(db_session, _TODAY)
    await db_session.commit()

    await _assert_budget(db_session, async_engine, label="wind_down")

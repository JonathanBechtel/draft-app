"""Integration tests for the Summer League scoreboard ingest (Job B step 0).

- ``resolve_target_competitions`` prefers a registered ``events`` row's
  ``calendar_ref``, falling back to the current year's competitions when no
  active event is registered yet.
- ``run_scoreboard_ingest`` creates/updates ``summer_league_games`` rows from
  a seeded scoreboard payload -- **no live network call**: the NBA Stats
  client is always given a fake ``curl_cffi``-compatible session.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import Event, EventCalendarSource, EventType
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.scoreboard_ingest import (
    resolve_target_competitions,
    run_scoreboard_ingest,
)

_N = {"i": 0}


class FakeResponse:
    """Minimal response object mirroring the curl_cffi shape the client reads."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured JSON payload."""
        return self.payload


class FakeSession:
    """Fake curl_cffi-compatible session that never touches the network.

    Routes responses by the request's ``LeagueID`` param so a single fake
    session can stand in for multiple competitions in one test.
    """

    def __init__(self, responses_by_league: dict[str, FakeResponse]) -> None:
        self.responses_by_league = responses_by_league
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        """Record the call and return the response registered for its LeagueID."""
        self.calls.append((url, params))
        league_id = params.get("LeagueID", "")
        if league_id not in self.responses_by_league:
            return FakeResponse({}, status_code=404)
        return self.responses_by_league[league_id]

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


def _schedule_payload(games_by_date: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "leagueSchedule": {
            "gameDates": [
                {"gameDate": game_date, "games": games}
                for game_date, games in games_by_date.items()
            ]
        }
    }


async def _competition(
    db: AsyncSession,
    *,
    year: int = 2026,
    league_id: str = "15",
    venue: str = "las_vegas",
) -> SummerLeagueCompetition:
    _N["i"] += 1
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"{venue}-{_N['i']}",
        display_name=f"{year} {venue}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    return comp


@pytest.mark.asyncio
async def test_resolve_target_competitions_falls_back_to_year_when_no_event(
    db_session: AsyncSession,
) -> None:
    """With no registered events row, resolution falls back to the target year."""
    this_year = await _competition(db_session, year=2026, league_id="15")
    await _competition(db_session, year=2025, league_id="15")
    await db_session.commit()

    resolved = await resolve_target_competitions(db_session, today=date(2026, 7, 9))

    assert [c.id for c in resolved] == [this_year.id]


@pytest.mark.asyncio
async def test_resolve_target_competitions_prefers_event_calendar_ref(
    db_session: AsyncSession,
) -> None:
    """A registered, active events row's calendar_ref overrides the year fallback."""
    vegas = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _competition(db_session, year=2026, league_id="13", venue="california")
    await db_session.flush()
    assert vegas.id is not None

    event = Event(
        key="summer_league",
        name="Summer League",
        event_type=EventType.PRO_SUMMER,
        calendar_source=EventCalendarSource.SCHEDULE,
        calendar_ref={"competition_ids": [vegas.id]},
        cohort_basis="slot_window",
        is_active=True,
    )
    db_session.add(event)
    await db_session.commit()

    resolved = await resolve_target_competitions(db_session, today=date(2026, 7, 9))

    assert [c.id for c in resolved] == [vegas.id]


@pytest.mark.asyncio
async def test_resolve_target_competitions_ignores_inactive_event(
    db_session: AsyncSession,
) -> None:
    """An inactive events row is not consulted; falls back to year matching."""
    this_year = await _competition(db_session, year=2026, league_id="15")
    other_venue = await _competition(db_session, year=2026, league_id="13")
    await db_session.flush()
    assert other_venue.id is not None

    event = Event(
        key="summer_league",
        name="Summer League",
        event_type=EventType.PRO_SUMMER,
        calendar_source=EventCalendarSource.SCHEDULE,
        calendar_ref={"competition_ids": [other_venue.id]},
        cohort_basis="slot_window",
        is_active=False,
    )
    db_session.add(event)
    await db_session.commit()

    resolved = await resolve_target_competitions(db_session, today=date(2026, 7, 9))

    assert {c.id for c in resolved} == {this_year.id, other_venue.id}


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_creates_and_updates_games(
    db_session: AsyncSession,
) -> None:
    """A seeded scoreboard payload creates a new game and updates an existing one."""
    comp = await _competition(db_session, year=2026, league_id="15")
    assert comp.id is not None

    existing = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id="existing-1",
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    db_session.add(existing)
    await db_session.commit()

    today = date(2026, 7, 9)
    tomorrow = date(2026, 7, 10)
    payload = _schedule_payload(
        {
            "07/09/2026 00:00:00": [
                {
                    "gameId": "existing-1",
                    "gameStatus": 2,
                    "gameStatusText": "Qtr 3 - 6:40",
                    "gameDateTimeUTC": "2026-07-09T22:00:00Z",
                }
            ],
            "07/10/2026 00:00:00": [
                {
                    "gameId": "new-1",
                    "gameStatus": 1,
                    "gameStatusText": "9:00 pm ET",
                    "gameDateTimeUTC": "2026-07-10T23:00:00Z",
                }
            ],
            "07/12/2026 00:00:00": [
                {
                    "gameId": "out-of-window",
                    "gameStatus": 1,
                    "gameDateTimeUTC": "2026-07-12T23:00:00Z",
                }
            ],
        }
    )
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    report = await run_scoreboard_ingest(db_session, today=today, client=client)
    await db_session.commit()

    assert report.competitions_checked == 1
    assert report.games_seen == 2
    assert report.games_created == 1
    assert report.games_updated == 1
    assert report.errors == []

    # No live network call: the fake session recorded exactly one GET.
    assert len(session.calls) == 1
    _, called_params = session.calls[0]
    assert called_params["LeagueID"] == "15"
    assert called_params["Season"] == "2026"

    refreshed_existing = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "existing-1"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert refreshed_existing.status == SummerLeagueGameStatus.IN_PROGRESS
    # tip_datetime is stored naive (UTC by convention, like the rest of this schema).
    assert refreshed_existing.tip_datetime == datetime(2026, 7, 9, 22, 0)
    assert refreshed_existing.game_date == today

    created = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "new-1"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert created.competition_id == comp.id
    assert created.status == SummerLeagueGameStatus.SCHEDULED
    assert created.tip_datetime == datetime(2026, 7, 10, 23, 0)
    assert created.game_date == tomorrow

    # The out-of-window game is not touched at all.
    missing = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "out-of-window"  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()
    assert missing is None


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_returns_empty_report_with_no_competitions(
    db_session: AsyncSession,
) -> None:
    """No resolved competitions means no fetches and an empty report."""
    report = await run_scoreboard_ingest(db_session, today=date(2099, 7, 9))

    assert report.competitions_checked == 0
    assert report.games_seen == 0
    assert report.games_created == 0
    assert report.games_updated == 0
    assert report.errors == []


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_continues_after_one_competition_fails(
    db_session: AsyncSession,
) -> None:
    """A failed fetch for one competition is recorded but does not abort the run."""
    ok_comp = await _competition(db_session, year=2026, league_id="15")
    await _competition(db_session, year=2026, league_id="13")
    await db_session.commit()
    assert ok_comp.id is not None

    payload = _schedule_payload(
        {
            "07/09/2026 00:00:00": [
                {
                    "gameId": "ok-game",
                    "gameStatus": 1,
                    "gameDateTimeUTC": "2026-07-09T22:00:00Z",
                }
            ]
        }
    )
    session = FakeSession(
        {
            "15": FakeResponse(payload),
            "13": FakeResponse({}, status_code=500),
        }
    )
    client = NBAStatsClient(session=session, max_retries=0)

    report = await run_scoreboard_ingest(
        db_session, today=date(2026, 7, 9), client=client
    )
    await db_session.commit()

    assert report.competitions_checked == 2
    assert report.games_created == 1
    assert len(report.errors) == 1
    assert "13" in report.errors[0]

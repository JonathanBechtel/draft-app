"""Integration tests for the Summer League scoreboard ingest (Job B step 0).

- ``resolve_target_competitions`` prefers a registered ``events`` row's
  ``calendar_ref``, falling back to the current year's competitions when no
  active event is registered yet.
- ``run_scoreboard_ingest`` creates/updates ``summer_league_games`` rows from
  a seeded scoreboard payload -- **no live network call**: the NBA Stats
  client is always given a fake ``curl_cffi``-compatible session.

Several tests below feed REAL captured ``scheduleleaguev2`` payloads to the
fake session rather than hand-authored dicts (repo convention -- see
``tests/unit/test_summer_league_scoreboard_ingest.py`` and
``test_summer_league_bracket.py`` for the same pattern):

* ``scheduleleaguev2_15_2024.json`` -- pre-existing repo fixture, the full
  real 2024 Las Vegas Summer League schedule (76 real Final games, 30 real
  NBA franchise team IDs).
* ``scheduleleaguev2_15_2026_live_pretip.json`` -- captured live from
  stats.nba.com on 2026-07-10, a genuine mid-event snapshot: real Final
  games from 2026-07-09 plus real not-yet-tipped Scheduled games spanning
  2026-07-10 through 2026-07-19.
* ``scoreboard_real_postponed_2021.json`` -- the one real "PPD" game found
  across an exhaustive capture sweep of every Summer League year/venue this
  ingest step covers (LeagueID 15, 2021 season, gameId 1522100005).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.event_desk import Event, EventCalendarSource, EventType
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.scoreboard_ingest import (
    resolve_target_competitions,
    run_scoreboard_ingest,
)
from tests.integration.perf._capture import count_queries

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
_REAL_FINAL_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2024.json"
_REAL_LIVE_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"
_REAL_POSTPONED_FIXTURE = _FIXTURE_ROOT / "scoreboard_real_postponed_2021.json"

_N = {"i": 0}


def _load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


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


async def _team(
    db: AsyncSession, competition: SummerLeagueCompetition, *, nba_stats_team_id: str
) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=nba_stats_team_id,
        raw_team_name=f"Team {nba_stats_team_id}",
        team_slug=f"team-{_N['i']}",
    )
    db.add(team)
    await db.flush()
    return team


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
                    "gameId": "far-horizon",
                    "gameStatus": 1,
                    "gameDateTimeUTC": "2026-07-12T23:00:00Z",
                }
            ],
        }
    )
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    # No target_dates: production default -- retains the full schedule.
    report = await run_scoreboard_ingest(db_session, today=today, client=client)
    await db_session.commit()

    assert report.competitions_checked == 1
    assert report.games_seen == 3
    assert report.games_created == 2
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

    # #529: the full-horizon default now retains a game more than two days out.
    far = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "far-horizon"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert far.game_date == date(2026, 7, 12)


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_respects_explicit_target_dates(
    db_session: AsyncSession,
) -> None:
    """An explicit target_dates override still narrows ingestion (tests/callers opt-in)."""
    comp = await _competition(db_session, year=2026, league_id="15")
    assert comp.id is not None

    payload = _schedule_payload(
        {
            "07/09/2026 00:00:00": [
                {
                    "gameId": "in-window",
                    "gameStatus": 1,
                    "gameDateTimeUTC": "2026-07-09T22:00:00Z",
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

    report = await run_scoreboard_ingest(
        db_session,
        today=date(2026, 7, 9),
        target_dates={date(2026, 7, 9)},
        client=client,
    )
    await db_session.commit()

    assert report.games_seen == 1
    assert report.games_created == 1

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
    assert report.unresolved_team_ids == []


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


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_untouched_competition_outside_the_event(
    db_session: AsyncSession,
) -> None:
    """Only the resolved (active-event) competition is fetched/ingested; the other is untouched."""
    resolved_comp = await _competition(
        db_session, year=2026, league_id="15", venue="vegas"
    )
    other_comp = await _competition(
        db_session, year=2026, league_id="13", venue="california"
    )
    await db_session.flush()
    assert resolved_comp.id is not None
    assert other_comp.id is not None

    event = Event(
        key="summer_league",
        name="Summer League",
        event_type=EventType.PRO_SUMMER,
        calendar_source=EventCalendarSource.SCHEDULE,
        calendar_ref={"competition_ids": [resolved_comp.id]},
        cohort_basis="slot_window",
        is_active=True,
    )
    db_session.add(event)
    await db_session.commit()

    payload = _schedule_payload(
        {
            "07/09/2026 00:00:00": [
                {
                    "gameId": "vegas-game",
                    "gameStatus": 1,
                    "gameDateTimeUTC": "2026-07-09T22:00:00Z",
                }
            ]
        }
    )
    # A response registered for LeagueID 13 too, proving it's simply never called
    # (resolve_target_competitions only returns the registered event's competition).
    session = FakeSession(
        {
            "15": FakeResponse(payload),
            "13": FakeResponse(
                _schedule_payload(
                    {
                        "07/09/2026 00:00:00": [
                            {
                                "gameId": "california-game",
                                "gameStatus": 1,
                                "gameDateTimeUTC": "2026-07-09T22:00:00Z",
                            }
                        ]
                    }
                )
            ),
        }
    )
    client = NBAStatsClient(session=session)

    report = await run_scoreboard_ingest(
        db_session, today=date(2026, 7, 9), client=client
    )
    await db_session.commit()

    assert report.competitions_checked == 1
    assert len(session.calls) == 1
    assert session.calls[0][1]["LeagueID"] == "15"

    games = (await db_session.execute(select(SummerLeagueGame))).scalars().all()
    assert [g.nba_stats_game_id for g in games] == ["vegas-game"]


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_real_final_game_links_teams_and_scores(
    db_session: AsyncSession,
) -> None:
    """A real Final game creates a genuine home/away matchup with score + team links.

    Fixture: 2024 Las Vegas Summer League, gameId 1522400001 (real payload) --
    Orlando Magic 106 (home) beat the Cleveland Cavaliers 79 (away).
    """
    comp = await _competition(db_session, year=2024, league_id="15")
    assert comp.id is not None
    home_team = await _team(db_session, comp, nba_stats_team_id="1610612753")  # ORL
    away_team = await _team(db_session, comp, nba_stats_team_id="1610612739")  # CLE
    await db_session.commit()

    payload = _load_fixture(_REAL_FINAL_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    report = await run_scoreboard_ingest(
        db_session, today=date(2024, 7, 12), client=client
    )
    await db_session.commit()

    assert report.errors == []
    assert "1610612753" not in report.unresolved_team_ids
    assert "1610612739" not in report.unresolved_team_ids

    game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert game.status == SummerLeagueGameStatus.FINAL
    assert game.status_text == "Final"
    assert game.home_nba_stats_team_id == "1610612753"
    assert game.away_nba_stats_team_id == "1610612739"
    assert game.home_score == 106
    assert game.away_score == 79
    assert game.home_team_entry_id == home_team.id
    assert game.away_team_entry_id == away_team.id


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_reports_unresolved_teams_without_fabricating_rows(
    db_session: AsyncSession,
) -> None:
    """An unresolvable real provider team ID is reported, never turned into a new team row."""
    comp = await _competition(db_session, year=2024, league_id="15")
    assert comp.id is not None
    # Deliberately no SummerLeagueTeamEntry rows seeded for this competition.

    payload = _load_fixture(_REAL_FINAL_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    report = await run_scoreboard_ingest(
        db_session, today=date(2024, 7, 12), client=client
    )
    await db_session.commit()

    assert "1610612753" in report.unresolved_team_ids  # ORL, real teamId
    assert "1610612739" in report.unresolved_team_ids  # CLE, real teamId

    team_rows = (
        (await db_session.execute(select(SummerLeagueTeamEntry))).scalars().all()
    )
    assert team_rows == []  # no parallel/fabricated team-entry rows

    game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    # The raw provider IDs are still retained even though resolution failed.
    assert game.home_nba_stats_team_id == "1610612753"
    assert game.away_nba_stats_team_id == "1610612739"
    assert game.home_team_entry_id is None
    assert game.away_team_entry_id is None


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_never_nulls_or_zeroes_existing_values(
    db_session: AsyncSession,
) -> None:
    """A re-poll that reports 0/unresolved fields never clobbers real prior values.

    Fixture provenance: the incoming payload is the real captured 2026
    pre-tip snapshot (``scheduleleaguev2_15_2026_live_pretip.json``, gameId
    1522600008 / gameCode "20260710/MILMIA": Milwaukee Bucks @ Miami Heat),
    where the game reports back as a not-yet-tipped 0-0 Scheduled game. The
    "prior, more-informed poll" state is a *seeded persisted DB row* (a real
    prior projection into ``summer_league_games``), NOT a fabricated provider
    payload -- a genuine mid-game provider snapshot of this specific game was
    not available at capture time (the game had not tipped when the fixtures
    were captured). This exercises the exact "later poll must not null/zero a
    value an earlier one set" contract without hand-authoring a fake provider
    dict; the positive "real score persists across a same-identity re-ingest"
    leg is covered by ``..._is_idempotent_on_reingest`` against the real 2024
    Final fixture.
    """
    comp = await _competition(db_session, year=2026, league_id="15")
    assert comp.id is not None
    home_team = await _team(
        db_session, comp, nba_stats_team_id="1610612748"
    )  # MIA (home)
    away_team = await _team(
        db_session, comp, nba_stats_team_id="1610612749"
    )  # MIL (away)
    assert home_team.id is not None
    assert away_team.id is not None

    existing = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id="1522600008",
        game_date=date(2026, 7, 10),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        status_text="Qtr 3 - 4:12",
        home_nba_stats_team_id="1610612748",
        away_nba_stats_team_id="1610612749",
        home_team_entry_id=home_team.id,
        away_team_entry_id=away_team.id,
        home_score=55,
        away_score=48,
    )
    db_session.add(existing)
    await db_session.commit()
    assert existing.id is not None

    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    await run_scoreboard_ingest(
        db_session,
        today=date(2026, 7, 10),
        target_dates={date(2026, 7, 10)},
        client=client,
    )
    await db_session.commit()

    refreshed = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.id == existing.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    # Same row, not a duplicate.
    assert (
        len(
            (
                await db_session.execute(
                    select(SummerLeagueGame).where(
                        SummerLeagueGame.nba_stats_game_id == "1522600008"  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )
        == 1
    )
    # The real feed's placeholder 0-0 never overwrote the real prior score.
    assert refreshed.home_score == 55
    assert refreshed.away_score == 48
    # The resolved team links survive too (still MIL/MIA -- unchanged).
    assert refreshed.home_team_entry_id == home_team.id
    assert refreshed.away_team_entry_id == away_team.id
    # Raw provider IDs are still refreshed with the honest current values.
    assert refreshed.home_nba_stats_team_id == "1610612748"
    assert refreshed.away_nba_stats_team_id == "1610612749"


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_real_postponed_game_never_marked_live(
    db_session: AsyncSession,
) -> None:
    """The one real captured "PPD" game ingests as POSTPONED, honest text retained.

    Fix #4: persisted as a real terminal POSTPONED status (not collapsed to
    SCHEDULED), so `select_active_window_games` excludes it by status alone.
    """
    comp = await _competition(db_session, year=2021, league_id="15")
    assert comp.id is not None

    payload = _load_fixture(_REAL_POSTPONED_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    await run_scoreboard_ingest(db_session, today=date(2021, 8, 8), client=client)
    await db_session.commit()

    game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522100005"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert game.status == SummerLeagueGameStatus.POSTPONED
    assert game.status_text == "PPD"


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_full_horizon_stores_games_beyond_two_days(
    db_session: AsyncSession,
) -> None:
    """The production default (no target_dates) retains the full real active-event schedule."""
    comp = await _competition(db_session, year=2026, league_id="15")
    assert comp.id is not None

    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    report = await run_scoreboard_ingest(
        db_session, today=date(2026, 7, 10), client=client
    )
    await db_session.commit()

    assert report.games_created == 76

    far_out = (
        (
            await db_session.execute(
                select(SummerLeagueGame).where(
                    SummerLeagueGame.game_date == date(2026, 7, 19)  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(far_out) > 0


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_is_idempotent_on_reingest(
    db_session: AsyncSession,
) -> None:
    """Re-ingesting the same real payload updates, not duplicates, every game.

    Fixture: the full real 2024 schedule (76 real Final games). Ingesting it
    twice must leave exactly 76 rows -- the second pass updates every row
    in place (exercising the score/team-link update branches) rather than
    creating duplicates.
    """
    comp = await _competition(db_session, year=2024, league_id="15")
    assert comp.id is not None
    home_team = await _team(db_session, comp, nba_stats_team_id="1610612753")  # ORL
    away_team = await _team(db_session, comp, nba_stats_team_id="1610612739")  # CLE
    await db_session.commit()

    payload = _load_fixture(_REAL_FINAL_FIXTURE)

    first_session = FakeSession({"15": FakeResponse(payload)})
    first_client = NBAStatsClient(session=first_session)
    first_report = await run_scoreboard_ingest(
        db_session, today=date(2024, 7, 12), client=first_client
    )
    await db_session.commit()
    assert first_report.games_created == 76
    assert first_report.games_updated == 0

    second_session = FakeSession({"15": FakeResponse(payload)})
    second_client = NBAStatsClient(session=second_session)
    second_report = await run_scoreboard_ingest(
        db_session, today=date(2024, 7, 12), client=second_client
    )
    await db_session.commit()
    assert second_report.games_created == 0
    assert second_report.games_updated == 76

    all_games = (await db_session.execute(select(SummerLeagueGame))).scalars().all()
    assert len(all_games) == 76

    game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert game.home_score == 106
    assert game.away_score == 79
    assert game.home_team_entry_id == home_team.id
    assert game.away_team_entry_id == away_team.id


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_team_resolution_is_a_single_batch_query(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Resolving every game's home/away team ID issues exactly one team-entries query.

    Fixture: the full real 2024 schedule (76 real games spanning 30 real NBA
    franchise team IDs). A per-game lookup would issue dozens of
    ``summer_league_team_entries`` queries; batching issues exactly one,
    regardless of how many games or distinct teams the payload carries.
    """
    comp = await _competition(db_session, year=2024, league_id="15")
    assert comp.id is not None
    await db_session.commit()

    payload = _load_fixture(_REAL_FINAL_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    with count_queries(async_engine) as captured:
        report = await run_scoreboard_ingest(
            db_session, today=date(2024, 7, 12), client=client
        )
        await db_session.commit()

    assert report.games_created == 76

    team_entry_queries = [s for s in captured if "summer_league_team_entries" in s]
    assert len(team_entry_queries) == 1, (
        f"expected exactly one summer_league_team_entries query, "
        f"got {len(team_entry_queries)}: {team_entry_queries}"
    )


@pytest.mark.asyncio
async def test_run_scoreboard_ingest_extends_competition_date_window_to_include_forward_scheduled_games(
    db_session: AsyncSession,
) -> None:
    """#527/#529: a schedule ingest of forward-scheduled games extends the window.

    This is what actually lets the Event Desk's opening-morning bootstrap
    (``_needs_scoreboard_bootstrap`` / ``_synthetic_calendar_dates`` in
    ``app/cli/sl_desk_tick.py``) go Active before any game is played --
    ``starts_on``/``ends_on`` previously stayed null forever because nothing
    ever populated them from a forward-looking schedule fetch. The
    competition starts with no configured window at all; after ingesting the
    real captured mid-event snapshot (Final games from 2026-07-09 plus
    not-yet-tipped Scheduled games through 2026-07-19), the window should
    span that fixture's actual date range.
    """
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2026 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None
    assert competition.starts_on is None
    assert competition.ends_on is None
    await db_session.commit()

    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    report = await run_scoreboard_ingest(
        db_session, today=date(2026, 7, 10), client=client
    )
    await db_session.commit()
    assert report.games_created == 76

    await db_session.refresh(competition)
    assert competition.starts_on == date(2026, 7, 9)
    assert competition.ends_on == date(2026, 7, 19)

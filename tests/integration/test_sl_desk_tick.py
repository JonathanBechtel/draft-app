"""Integration tests for the Summer League Desk hourly tick orchestrator (#516).

`scripts/sl_desk_tick.py` wires scoreboard ingest -> normalize -> grades ->
storylines -> commentary -> ``event_desk_state`` upsert into one call
(``run_desk_tick``). These tests don't re-verify any individual step's
algorithm (that's each sibling ticket's own test file) -- they prove the
*orchestration*: a seeded schedule + player-season log + active T1 baseline
produces T2/T3/T4/``event_desk_state`` rows end to end, re-running is
idempotent, and an off-window tick touches nothing but the freshness stamp.

No live network calls: the NBA Stats client is always given a fake
``curl_cffi``-compatible session (mirrors
``tests/integration/test_summer_league_scoreboard_ingest.py``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import (
    Event,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
)
from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
    SummerLeagueDeskStoryline,
    SummerLeagueDeskTriggerType,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.nba_stats_client import NBAStatsClient
from scripts.sl_desk_tick import run_desk_tick

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


class FakeResponse:
    """Minimal response object mirroring the curl_cffi shape the client reads."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured JSON payload."""
        return self.payload


class FakeSession:
    """Fake curl_cffi-compatible session that never touches the network."""

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


def _empty_schedule_payload() -> dict[str, Any]:
    return {"leagueSchedule": {"gameDates": []}}


async def _seed_competition(
    db: AsyncSession,
    *,
    year: int,
    league_id: str = "15",
    venue_slug: str = "las_vegas",
    starts_on: date | None = None,
    ends_on: date | None = None,
) -> SummerLeagueCompetition:
    idx = _next_idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"{venue_slug}-{idx}",
        display_name=f"{year} {venue_slug}",
        starts_on=starts_on or date(year, 7, 1),
        ends_on=ends_on or date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


def _schedule_payload_with_game(
    *, game_id: str, tip_iso: str, status: int = 1, status_text: str = "Scheduled"
) -> dict[str, Any]:
    """A ``scheduleleaguev2``-shaped payload carrying exactly one game."""
    return {
        "leagueSchedule": {
            "gameDates": [
                {
                    "gameDate": tip_iso,
                    "games": [
                        {
                            "gameId": game_id,
                            "gameStatus": status,
                            "gameStatusText": status_text,
                            "gameDateTimeUTC": tip_iso,
                        }
                    ],
                }
            ]
        }
    }


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueCompetition
) -> SummerLeagueTeamEntry:
    idx = _next_idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"t-{idx}",
        raw_team_name=f"Team {idx}",
        team_slug=f"team-{idx}",
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
    idx = _next_idx()
    assert competition.id is not None
    assert home.id is not None
    assert away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"desk-tick-game-{idx}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=status,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _seed_player(
    db: AsyncSession, *, name: str, draft_round: int | None, draft_pick: int | None
) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name,
        last_name="Test",
        display_name=f"{name} Test",
        draft_year=2026,
        draft_round=draft_round,
        draft_pick=draft_pick,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _roster_player(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
) -> None:
    idx = _next_idx()
    assert competition.id is not None
    assert team.id is not None
    assert player.id is not None
    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"src-{idx}",
        raw_player_name=player.display_name or "Test Player",
        normalized_name=(player.display_name or "test player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None

    participation = SummerLeagueParticipation(
        competition_id=competition.id,
        team_entry_id=team.id,
        source_player_id=source_player.id,
        player_id=player.id,
        roster_status=AffiliationStatus.ACTIVE,
    )
    db.add(participation)
    await db.flush()


async def _seed_season(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    player: PlayerMaster,
    year: int,
    gmsc: float,
    minutes: float,
    gp: int,
) -> None:
    assert competition.id is not None
    assert player.id is not None
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=year,
            venue_slug=competition.venue_slug,
            gp=gp,
            minutes=minutes,
            gmsc=gmsc,
        )
    )
    await db.flush()


async def _seed_baseline(
    db: AsyncSession, *, baseline_version: str, cohort_key: str = "slot:1-4"
) -> None:
    baseline = SummerLeagueCohortBaseline(
        baseline_version=baseline_version,
        is_active=True,
        cohort_key=cohort_key,
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        metric="gmsc",
        grain=SummerLeagueDeskGrain.EVENT,
        venue_scope="all",
        season_range="2017-2025",
        min_minutes=40.0,
        n_members=20,
        breakpoints={"0": 10.0, "25": 30.0, "50": 50.0, "75": 70.0, "100": 90.0},
        mean_value=50.0,
        median_value=50.0,
    )
    db.add(baseline)
    await db.flush()


async def _event_desk_state_for(
    db: AsyncSession, *, key: str = "summer_league"
) -> EventDeskState:
    event = (
        await db.execute(select(Event).where(Event.key == key))  # type: ignore[arg-type]
    ).scalar_one()
    assert event.id is not None
    return (
        await db.execute(
            select(EventDeskState).where(EventDeskState.event_id == event.id)  # type: ignore[arg-type]
        )
    ).scalar_one()


async def test_desk_tick_writes_t2_t3_t4_and_event_desk_state_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    """One tick over a seeded schedule + season log + active baseline writes T2/T3/T4/state.

    Also proves idempotency: re-running the tick over the same data does not
    duplicate any T2/T3/T4 row.
    """
    now = datetime(2026, 7, 10, 20, 0)  # 4:00pm ET (EDT, UTC-4)
    year = 2026

    competition = await _seed_competition(db_session, year=year)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
    )

    player = await _seed_player(db_session, name="Rookie", draft_round=1, draft_pick=1)
    await _roster_player(db_session, competition, home, player)
    await _seed_season(
        db_session,
        competition=competition,
        player=player,
        year=year,
        gmsc=75.0,
        minutes=150.0,
        gp=5,
    )

    baseline_version = "sl-desk-tick-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    await db_session.commit()

    session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    client = NBAStatsClient(session=session)

    result = await run_desk_tick(db_session, now=now, client=client)
    await db_session.commit()

    assert result.dormant is False
    assert result.daily_state == EventDailyState.RECAP
    assert result.baseline_version == baseline_version
    assert competition.id is not None
    assert player.id is not None
    assert result.graded_player_ids == (player.id,)

    # T2 -- one graded, ungated row with a persisted percentile Fact.
    grade_rows = (
        (
            await db_session.execute(
                select(SummerLeagueDeskPlayerGrade).where(
                    SummerLeagueDeskPlayerGrade.player_id == player.id,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.competition_id == competition.id,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(grade_rows) == 1
    grade_row = grade_rows[0]
    assert grade_row.cohort_key == "slot:1-4"
    assert grade_row.subject_value == 75.0
    assert grade_row.gated is False
    assert grade_row.facts
    assert grade_row.facts[0]["kind"] == "percentile"

    # T3 -- the debut trigger fired for the roster's only (first-ever-logged) player.
    storyline_rows = (
        (
            await db_session.execute(
                select(SummerLeagueDeskStoryline).where(
                    SummerLeagueDeskStoryline.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(storyline_rows) == 1
    assert storyline_rows[0].trigger_type == SummerLeagueDeskTriggerType.DEBUT
    assert storyline_rows[0].subject_player_id == player.id

    # T4 -- one slate row for the day's only game, flagged hero, carrying the same Fact.
    slate_row = (
        await db_session.execute(
            select(SummerLeagueDeskSlate).where(
                SummerLeagueDeskSlate.game_id == game.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert slate_row.is_hero is True
    assert slate_row.facts
    assert slate_row.facts[0]["kind"] == "percentile"

    # event_desk_state -- active/recap, freshness stamped to `now`.
    state = await _event_desk_state_for(db_session)
    assert state.lifecycle_phase == EventLifecyclePhase.ACTIVE
    assert state.daily_state == EventDailyState.RECAP
    assert state.freshness_tick_at == now

    # Re-running over identical data must not duplicate T2/T3/T4 rows.
    second_session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    second_client = NBAStatsClient(session=second_session)
    second_result = await run_desk_tick(db_session, now=now, client=second_client)
    await db_session.commit()

    assert second_result.daily_state == EventDailyState.RECAP
    grade_rows_after = (
        (
            await db_session.execute(
                select(SummerLeagueDeskPlayerGrade).where(
                    SummerLeagueDeskPlayerGrade.player_id == player.id,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.competition_id == competition.id,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(grade_rows_after) == 1
    storyline_rows_after = (
        (
            await db_session.execute(
                select(SummerLeagueDeskStoryline).where(
                    SummerLeagueDeskStoryline.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(storyline_rows_after) == 1
    slate_rows_after = (
        (
            await db_session.execute(
                select(SummerLeagueDeskSlate).where(
                    SummerLeagueDeskSlate.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(slate_rows_after) == 1


async def test_desk_tick_raises_when_no_active_baseline(
    db_session: AsyncSession,
) -> None:
    """An in-window tick with no active T1 baseline fails loudly (Job A hasn't run)."""
    now = datetime(2026, 7, 10, 20, 0)
    year = 2026

    competition = await _seed_competition(db_session, year=year)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
    )
    await db_session.commit()

    session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    client = NBAStatsClient(session=session)

    with pytest.raises(RuntimeError, match="No active Summer League cohort baseline"):
        await run_desk_tick(db_session, now=now, client=client)


async def test_desk_tick_off_window_is_a_no_op(db_session: AsyncSession) -> None:
    """A tick with zero known Summer League games (fully off-window) touches nothing.

    No fake NBA Stats client is injected: if the implementation regressed and
    proceeded past the dormancy guard, `run_scoreboard_ingest` would try to
    open a real client and this test would fail/hang rather than silently
    passing -- an extra guarantee the inert path was actually taken.
    """
    now = datetime(2099, 1, 15, 12, 0)

    result = await run_desk_tick(db_session, now=now)
    await db_session.commit()

    assert result.dormant is True
    assert result.daily_state is None
    assert result.graded_player_ids == ()
    assert result.storyline_results == {}

    assert (
        await db_session.execute(select(SummerLeagueDeskPlayerGrade))
    ).scalars().all() == []
    assert (
        await db_session.execute(select(SummerLeagueDeskStoryline))
    ).scalars().all() == []
    assert (
        await db_session.execute(select(SummerLeagueDeskSlate))
    ).scalars().all() == []
    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []

    state = await _event_desk_state_for(db_session)
    assert state.lifecycle_phase == EventLifecyclePhase.DORMANT
    assert state.daily_state is None
    assert state.freshness_tick_at == now


async def test_desk_tick_bootstraps_scoreboard_on_first_morning_with_no_games_yet(
    db_session: AsyncSession,
) -> None:
    """#527: a fresh competition with zero games, ticked on its very first morning, self-bootstraps.

    `_resolve_daily_state` alone would see an empty calendar (zero
    `summer_league_games` rows anywhere for this competition) and resolve
    dormant -- exactly the chicken-and-egg gap #527 fixes: step 0 (scoreboard
    ingest) is what would create that anchor, but the dormancy guard was
    skipping straight past it. This proves the tick instead recognizes the
    first-morning/pre-roll window via the competition's configured
    ``starts_on``/``ends_on``, runs the scoreboard bootstrap once, and
    produces a non-dormant Morning Card (``PREVIEW``) slate.
    """
    year = 2026
    competition = await _seed_competition(
        db_session,
        year=year,
        starts_on=date(year, 7, 10),
        ends_on=date(year, 7, 20),
    )

    baseline_version = "sl-desk-tick-bootstrap-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    await db_session.commit()

    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []

    now = datetime(
        2026, 7, 10, 19, 0
    )  # 3:00pm ET (EDT) -- after the Morning flip, before tip.
    payload = _schedule_payload_with_game(
        game_id="desk-tick-bootstrap-game-1",
        tip_iso="2026-07-10T23:00:00Z",  # 7:00pm ET tip.
    )
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    result = await run_desk_tick(db_session, now=now, client=client)
    await db_session.commit()

    assert result.dormant is False
    assert result.bootstrapped is True
    assert result.daily_state == EventDailyState.PREVIEW
    assert result.scoreboard_report is not None
    assert result.scoreboard_report.games_created == 1

    games = (await db_session.execute(select(SummerLeagueGame))).scalars().all()
    assert len(games) == 1
    assert games[0].competition_id == competition.id
    assert games[0].game_date == date(2026, 7, 10)

    # T4 -- a Morning slate row exists for the newly-bootstrapped game.
    slate_rows = (
        (await db_session.execute(select(SummerLeagueDeskSlate))).scalars().all()
    )
    assert len(slate_rows) == 1
    assert slate_rows[0].game_id == games[0].id

    state = await _event_desk_state_for(db_session)
    assert state.lifecycle_phase == EventLifecyclePhase.ACTIVE
    assert state.daily_state == EventDailyState.PREVIEW


async def test_desk_tick_far_off_competition_stays_dormant_without_bootstrap(
    db_session: AsyncSession,
) -> None:
    """A current-year competition exists but `now` is nowhere near its window -- stays inert.

    No fake NBA Stats client is injected, same guarantee as
    `test_desk_tick_off_window_is_a_no_op`: if `_needs_scoreboard_bootstrap`
    regressed and fired for a competition that's merely registered (not
    actually imminent per its `starts_on`/`ends_on`), `run_scoreboard_ingest`
    would try to open a real client and this test would fail/hang.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=date(year, 7, 10),
        ends_on=date(year, 7, 20),
    )
    await db_session.commit()

    # Three months before `announce_horizon_days` (14) even opens the window.
    now = datetime(year, 3, 1, 12, 0)

    result = await run_desk_tick(db_session, now=now)
    await db_session.commit()

    assert result.dormant is True
    assert result.bootstrapped is False
    assert result.daily_state is None
    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []

    state = await _event_desk_state_for(db_session)
    assert state.lifecycle_phase == EventLifecyclePhase.DORMANT
    assert state.daily_state is None

"""Integration tests for the Summer League Desk hourly tick orchestrator (#516).

`scripts/sl_desk_tick.py` wires scoreboard ingest -> targeted live raw
refresh -> normalize -> grades -> storylines -> commentary -> render/state
freshness (``event_desk_state`` upsert) into one call (``run_desk_tick``).
These tests don't re-verify any individual step's algorithm (that's each
sibling ticket's own test file) -- they prove the *orchestration*: a seeded
schedule + player-season log + active T1 baseline produces
T2/T3/T4/``event_desk_state`` rows end to end, re-running is idempotent, an
off-window tick touches nothing but the freshness stamp, a live tick's
targeted raw refresh runs in the correct order and never lets a partial
snapshot finalize a game (#530), and a required refresh failure aborts
before the freshness stamp is claimed.

No live network calls: the NBA Stats client is always given a fake
``curl_cffi``-compatible session (mirrors
``tests/integration/test_summer_league_scoreboard_ingest.py``).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
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
from app.services.summer_league.raw_ingestion import GAME_ENDPOINTS
from scripts.sl_desk_tick import run_desk_tick

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
_REAL_LIVE_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"

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


class SequencedFakeSession:
    """Fake curl_cffi-compatible session, routed by endpoint (and PlayerOrTeam).

    `FakeSession` above (keyed only by LeagueID) is fine when a test's whole
    tick shares one schedule-shaped payload. #530's targeted live raw
    refresh step (#531) issues several *different* NBA Stats endpoint calls
    within a single tick -- ``scheduleleaguev2`` (scoreboard), plus
    ``leaguegamelog`` (once each for team/player rows) and five per-game
    endpoints (targeted refresh) -- so this fake distinguishes by the
    request URL's endpoint name and, for ``leaguegamelog``, by the
    ``PlayerOrTeam`` param. Each registered key holds a *sequence* of
    responses served in call order (the last one repeats once exhausted),
    so a test can prove a second tick issued a genuinely fresh round of
    calls rather than reusing a cached response. Any endpoint/key combo not
    registered gets a trivial ``{"resultSets": []}`` (200) by default.
    """

    def __init__(
        self,
        responses: dict[tuple[str, str], list[FakeResponse]],
        *,
        default: FakeResponse | None = None,
    ) -> None:
        self._responses = responses
        self._default = default or FakeResponse({"resultSets": []})
        self._call_index: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        """Record the call and return the next response for its (endpoint, key)."""
        clean_params = dict(params)
        self.calls.append((url, clean_params))
        endpoint = url.rsplit("/", 1)[-1]
        key = (endpoint, clean_params.get("PlayerOrTeam", ""))
        sequence = self._responses.get(key)
        if not sequence:
            return self._default
        idx = self._call_index.get(key, 0)
        self._call_index[key] = idx + 1
        return sequence[min(idx, len(sequence) - 1)]

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


def _load_real_2026_schedule_fixture() -> dict[str, Any]:
    """Load the real captured 2026 live-pretip schedule fixture (see module docstring below)."""
    return json.loads(_REAL_LIVE_FIXTURE.read_text())  # type: ignore[no-any-return]


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
    tmp_path: Path,
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

    result = await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
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
    second_result = await run_desk_tick(
        db_session, now=now, raw_root=tmp_path, client=second_client
    )
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
    tmp_path: Path,
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
    # `FakeSession` (keyed only by LeagueID) would also serve the schedule
    # payload back for the box-score endpoints step 1's live refresh fires
    # for the newly-bootstrapped Scheduled game -- those requests carry no
    # `LeagueID` param at all (`build_boxscore_params`), so a LeagueID-only
    # fake would 404 them. This test isn't exercising the live-refresh path,
    # so route by endpoint instead and let every unregistered endpoint (the
    # season gamelogs and all five per-game endpoints) succeed with a
    # trivial empty payload.
    session = SequencedFakeSession({("scheduleleaguev2", ""): [FakeResponse(payload)]})
    client = NBAStatsClient(session=session)

    result = await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
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


async def test_desk_tick_two_sequential_ticks_over_real_schedule_never_finalize_a_scheduled_game(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#530 vertical two-snapshot test: two sequential ticks over a REAL captured 2026 schedule.

    Fixture provenance (repo guardrail on this ticket: the provider fixture
    driving this test must be a REAL captured NBA payload, not a
    hand-authored dict): ``scheduleleaguev2_15_2026_live_pretip.json`` was
    captured live from stats.nba.com (LeagueID 15, Season 2026) on
    2026-07-10 ~19:34 UTC -- seven real Final games (2026-07-09 19:30
    through 2026-07-10 03:00 UTC) plus real not-yet-tipped Scheduled games
    (2026-07-10 through 2026-07-19). This is the exact same fixture #531's
    own tests use (``test_summer_league_live_ingestion.py``); the reference
    `now` and the six-game active-window selection asserted below are lifted
    directly from that ticket's own proven expectations, not re-derived.

    No genuinely in-progress (``gameStatus == 2``) capture exists anywhere on
    this branch -- checked the SL fixtures dir, #529/#531's own captured
    assets, and `scraper/output/` before writing this test; NBA's real
    ``leaguegamelog``/per-game boxscore endpoints also have no captured 2026
    response in this repo (this sandbox has no live network access to fetch
    one). Per the ticket's guardrail, this test therefore proves the
    regression-prevention guarantee using the real states that ARE
    available -- a real Scheduled game across two sequential ticks (proving
    it is never promoted, and that the second tick genuinely re-fetches
    rather than reusing a cached snapshot) and a real Final game (proving
    scoreboard's own provider truth establishes Final, and that it stays
    monotonic across the second tick) -- rather than hand-authoring a fake
    mid-game snapshot. Every assertion below reads only real
    scoreboard-derived state (`SummerLeagueGame.status`/scores) plus the
    live-refresh call log; the per-game/season-gamelog endpoints (which have
    no real 2026 capture available) are served a trivial empty
    ``{"resultSets": []}`` and nothing in this test depends on their
    content. (A hand-authored-but-honestly-labeled "scores advance while
    non-Final" case, where fabricating raw content is unavoidable, lives in
    the normalization-level persisted-case tier instead --
    ``tests/integration/test_summer_league_normalization.py::test_normalize_competition_games_advances_scores_across_partial_passes_while_non_final``.)
    """
    year = 2026
    competition = await _seed_competition(db_session, year=year, league_id="15")
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    # An anchor game (arbitrary ID, unrelated to the real fixture's IDs) so
    # `_resolve_daily_state` resolves non-dormant on the very first call,
    # keeping this test focused on the live-refresh/status-resolution
    # behavior rather than the (separately covered) #527 bootstrap path.
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
    )
    baseline_version = "sl-desk-tick-live-refresh-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    await db_session.commit()

    real_schedule = _load_real_2026_schedule_fixture()
    session = SequencedFakeSession(
        {("scheduleleaguev2", ""): [FakeResponse(real_schedule)]}
    )
    client = NBAStatsClient(session=session)

    # Matches #531's own reference "now"
    # (`test_summer_league_live_ingestion.py`): six real Scheduled games tip
    # inside [now-6h, now+6h]; all seven real Final games tip well before
    # the window opens, and the real game tipping just past the window
    # (1522600014) is correctly excluded.
    now = datetime(2026, 7, 10, 19, 30)

    first = await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
    await db_session.commit()

    assert first.dormant is False
    assert first.live_refresh_report is not None
    assert first.live_refresh_report.selected == 6
    assert first.live_refresh_report.groups == 1
    assert first.live_refresh_report.required_errors == 0

    async def _game_status_and_scores(
        nba_stats_game_id: str,
    ) -> tuple[SummerLeagueGameStatus, int | None, int | None]:
        row = (
            await db_session.execute(
                select(SummerLeagueGame).where(
                    SummerLeagueGame.nba_stats_game_id == nba_stats_game_id  # type: ignore[arg-type]
                )
            )
        ).scalar_one()
        return row.status, row.home_score, row.away_score

    scheduled_status, scheduled_home, _ = await _game_status_and_scores("1522600008")
    assert scheduled_status == SummerLeagueGameStatus.SCHEDULED
    assert scheduled_home is None  # real pretip score

    final_status, final_home, final_away = await _game_status_and_scores("1522600001")
    assert final_status == SummerLeagueGameStatus.FINAL
    assert final_home == 92
    assert final_away == 105

    first_box_calls = [
        call for call in session.calls if call[1].get("GameID") == "1522600008"
    ]
    assert len(first_box_calls) == len(GAME_ENDPOINTS)

    # Second sequential tick, 30 minutes later -- the window has shifted
    # forward, so a seventh real Scheduled game (1522600014, tipping exactly
    # at the new window boundary) now also qualifies; the original six,
    # including 1522600008, remain selected too.
    second = await run_desk_tick(
        db_session,
        now=now + timedelta(minutes=30),
        raw_root=tmp_path,
        client=client,
    )
    await db_session.commit()

    assert second.dormant is False
    assert second.live_refresh_report is not None
    assert second.live_refresh_report.selected == 7
    assert second.live_refresh_report.required_errors == 0

    second_box_calls = [
        call for call in session.calls if call[1].get("GameID") == "1522600008"
    ]
    # The second tick issued a genuinely fresh round of per-game calls --
    # not a cache hit -- proving it read the second raw snapshot, not the
    # first (the targeted refresh always forces a re-fetch, #531).
    assert len(second_box_calls) == 2 * len(GAME_ENDPOINTS)

    scheduled_status_after, _, _ = await _game_status_and_scores("1522600008")
    assert scheduled_status_after == SummerLeagueGameStatus.SCHEDULED

    (
        final_status_after,
        final_home_after,
        final_away_after,
    ) = await _game_status_and_scores("1522600001")
    assert final_status_after == SummerLeagueGameStatus.FINAL
    assert final_home_after == 92
    assert final_away_after == 105


async def test_desk_tick_required_live_refresh_failure_aborts_before_claiming_fresh_state(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#530: a failed *required* season-gamelog fetch aborts the tick before step 6.

    A live tick's targeted raw refresh (#531) selected one real Scheduled
    game this tick, but the season leaguegamelog fetch every game in that
    (year, LeagueID) group depends on failed outright (a non-retryable HTTP
    404 here, standing in for any hard network/provider failure). The tick
    must raise before writing any T2-T4 row or the `event_desk_state`
    freshness stamp -- the caller's transaction then rolls back cleanly and
    the next scheduled tick retries from the prior good state, rather than
    silently claiming this tick's data is current.
    """
    year = 2026
    competition = await _seed_competition(db_session, year=year, league_id="15")
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
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 20, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    baseline_version = "sl-desk-tick-required-failure-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    await db_session.commit()

    # Scoreboard succeeds (empty schedule -- doesn't touch the seeded
    # games); the required season leaguegamelog fetch the live-refresh step
    # needs fails outright (404, non-retryable).
    session = SequencedFakeSession(
        {
            ("scheduleleaguev2", ""): [FakeResponse(_empty_schedule_payload())],
            ("leaguegamelog", "T"): [FakeResponse({}, status_code=404)],
        }
    )
    client = NBAStatsClient(session=session)

    now = datetime(2026, 7, 10, 19, 30)

    with pytest.raises(
        RuntimeError, match="Required Summer League live raw refresh failed"
    ):
        await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)

    # No event_desk_state row was ever written this call -- the tick never
    # reached step 6.
    states = (await db_session.execute(select(EventDeskState))).scalars().all()
    assert states == []


async def test_desk_tick_required_box_score_failure_aborts_before_claiming_fresh_state(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A failed *critical* box-score fetch also aborts the tick, not just a gamelog failure.

    Before this fix, only a required season-gamelog failure counted toward
    ``LiveIngestionReport.required_errors``; a failed ``boxscoretraditionalv2``
    fetch (the actual player box-score line) landed only in the non-blocking
    ``errors`` count, so #530's abort gate never fired and the tick could
    commit with the OLD on-disk snapshot (``force=True`` never overwrites on
    a failed fetch) silently treated as fresh. This proves the fix: a
    critical box-score endpoint failure for the one selected live game now
    aborts the tick the same way a required-gamelog failure does, before
    writing any T2-T4 row or the ``event_desk_state`` freshness stamp.
    """
    year = 2026
    competition = await _seed_competition(db_session, year=year, league_id="15")
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 20, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    baseline_version = "sl-desk-tick-box-score-failure-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    await db_session.commit()

    # Scoreboard succeeds (empty schedule -- doesn't touch the seeded game);
    # the required season gamelogs succeed (default empty-but-valid
    # response, unregistered here); the critical boxscoretraditionalv2 fetch
    # for the one selected live game fails outright (404, non-retryable).
    session = SequencedFakeSession(
        {
            ("scheduleleaguev2", ""): [FakeResponse(_empty_schedule_payload())],
            ("boxscoretraditionalv2", ""): [FakeResponse({}, status_code=404)],
        }
    )
    client = NBAStatsClient(session=session)

    now = datetime(2026, 7, 10, 19, 30)

    with pytest.raises(
        RuntimeError, match="Required Summer League live raw refresh failed"
    ):
        await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)

    # No event_desk_state row was ever written this call -- the tick never
    # reached step 6, exactly as it would for a required-gamelog failure.
    states = (await db_session.execute(select(EventDeskState))).scalars().all()
    assert states == []


async def test_desk_tick_optional_pbp_or_shotchart_failure_does_not_abort(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A failed shot-chart fetch stays non-blocking -- the tick still completes.

    Mirrors the box-score-failure abort test above, but the failing endpoint
    is ``shotchartdetail`` -- classified non-required
    (``is_required_game_endpoint``), so ``required_errors`` stays 0 and the
    tick reaches step 6/7 and stamps freshness normally, unlike a
    box-score/gamelog failure.
    """
    year = 2026
    competition = await _seed_competition(db_session, year=year, league_id="15")
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 20, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    baseline_version = "sl-desk-tick-shotchart-failure-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    await db_session.commit()

    session = SequencedFakeSession(
        {
            ("scheduleleaguev2", ""): [FakeResponse(_empty_schedule_payload())],
            ("shotchartdetail", ""): [FakeResponse({}, status_code=404)],
        }
    )
    client = NBAStatsClient(session=session)

    now = datetime(2026, 7, 10, 19, 30)

    result = await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)

    assert result.live_refresh_report is not None
    assert result.live_refresh_report.required_errors == 0
    assert result.live_refresh_report.errors >= 1
    states = (await db_session.execute(select(EventDeskState))).scalars().all()
    assert len(states) >= 1

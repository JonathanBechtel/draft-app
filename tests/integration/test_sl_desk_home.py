"""Integration tests for the Summer League Desk read service + `/` wiring (#508).

Proves the request-time resolution contract (behavior spec §2): `daily_state`
is resolved fresh by the pure state-machine resolvers on every call, never read
back from `event_desk_state`'s own `daily_state` column. Also proves the
payload shape per state, the section present/absent contract, the off-window
short-circuit, the quiet-slate fallback, and each state's query-count budget
(`tests/integration/perf/budgets.py::DESK_HOME_QUERY_BUDGETS`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.schemas.event_desk import Event, EventDailyState, EventDeskState
from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrade,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.event_desk.registry import sync_summer_league_event
from app.services.summer_league.desk_read import get_desk_payload
from app.services.summer_league.metrics import game_score_line
from app.services.summer_league.nba_stats_client import NBAStatsClient
from scripts.sl_desk_tick import run_desk_tick
from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import DESK_HOME_QUERY_BUDGETS

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


class _FakeResponse:
    """Minimal response object mirroring the curl_cffi shape the client reads."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured JSON payload."""
        return self.payload


class _FakeSession:
    """Fake curl_cffi-compatible session that never touches the network."""

    def __init__(self) -> None:
        self._response = _FakeResponse({"leagueSchedule": {"gameDates": []}})

    def get(self, url: str, params: dict[str, str]) -> _FakeResponse:
        """Always return an empty schedule -- games are seeded directly by tests."""
        return self._response

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


def _fake_client() -> NBAStatsClient:
    return NBAStatsClient(session=_FakeSession())


async def _seed_competition(
    db: AsyncSession, *, year: int, venue_slug: str = "las_vegas"
) -> SummerLeagueCompetition:
    idx = _next_idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug=f"{venue_slug}-{idx}",
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueCompetition
) -> SummerLeagueTeamEntry:
    idx = _next_idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"t-{idx}",
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
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
    tip_datetime: datetime | None,
    status: SummerLeagueGameStatus,
    home_score: int | None = None,
    away_score: int | None = None,
) -> SummerLeagueGame:
    idx = _next_idx()
    assert competition.id is not None
    assert home.id is not None
    assert away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"desk-home-game-{idx}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=status,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _seed_player(
    db: AsyncSession,
    *,
    name: str,
    draft_year: int | None,
    draft_round: int | None,
    draft_pick: int | None,
    position: str = "G",
) -> PlayerMaster:
    idx = _next_idx()
    player = PlayerMaster(
        first_name=name,
        last_name=f"Test{idx}",
        display_name=f"{name} Test{idx}",
        draft_year=draft_year,
        draft_round=draft_round,
        draft_pick=draft_pick,
        position=position,
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
) -> SummerLeagueSourcePlayer:
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
    return source_player


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
    db: AsyncSession,
    *,
    baseline_version: str,
    cohort_key: str = "slot:1-4",
    grain: SummerLeagueDeskGrain = SummerLeagueDeskGrain.EVENT,
) -> None:
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key=cohort_key,
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=grain,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=40.0,
            n_members=20,
            breakpoints={"0": 10.0, "25": 30.0, "50": 50.0, "75": 70.0, "100": 90.0},
            mean_value=50.0,
            median_value=50.0,
        )
    )
    await db.flush()


async def _seed_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    source_player: SummerLeagueSourcePlayer,
    player: PlayerMaster,
    pts: int = 20,
    reb: int = 8,
    ast: int = 5,
) -> None:
    assert competition.id is not None
    assert game.id is not None
    assert team.id is not None
    assert source_player.id is not None
    assert player.id is not None
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=player.display_name or "Player",
            minutes_seconds=1800,
            pts=pts,
            fgm=8,
            fga=15,
            ftm=2,
            fta=2,
            oreb=1,
            dreb=7,
            reb=reb,
            ast=ast,
            stl=1,
            blk=0,
            tov=2,
            pf=2,
        )
    )
    await db.flush()


async def _event_desk_state_for(
    db: AsyncSession, *, key: str = "summer_league"
) -> EventDeskState:
    event = (await db.execute(select(Event).where(Event.key == key))).scalar_one()  # type: ignore[arg-type]
    assert event.id is not None
    return (
        await db.execute(
            select(EventDeskState).where(EventDeskState.event_id == event.id)  # type: ignore[arg-type]
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# Off-window
# --------------------------------------------------------------------------- #
async def test_off_window_returns_none_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """No `events` row at all (tick never ran) -> `None`, one cheap lookup."""
    with count_queries(async_engine) as captured:
        payload = await get_desk_payload(db_session, now=datetime(2026, 7, 10, 20, 0))

    assert payload is None
    budget = DESK_HOME_QUERY_BUDGETS["off_window"]
    assert len(captured) <= budget, (
        f"off-window issued {len(captured)} queries, over budget of {budget}: {captured}"
    )


# --------------------------------------------------------------------------- #
# The critical contract: state resolved at request time, not from the last tick
# --------------------------------------------------------------------------- #
async def test_daily_state_resolved_at_request_time_not_from_stale_tick(
    db_session: AsyncSession,
) -> None:
    """A 7:05pm ET tip renders Live even though the last tick (6:55pm) saw Preview.

    `event_desk_state.daily_state` is written by the framework controller at
    tick time and is never updated between ticks. This proves the read service
    ignores that stored verdict entirely and re-derives Live via the
    scheduled-tip fallback (behavior spec §2) purely from `now` + the game's
    (still-`SCHEDULED`) status.
    """
    year = 2026
    # 7:00pm ET on 2026-07-10 (EDT, UTC-4) -> 23:00 naive UTC.
    tip = datetime(2026, 7, 10, 23, 0)

    competition = await _seed_competition(db_session, year=year)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=tip,
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    player = await _seed_player(
        db_session, name="Rookie", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, home, player)
    baseline_version = "desk-home-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=player.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=60.0,
            pctl=70.0,
            grade=SummerLeagueDeskGrade.WARM,
            n_cohort=20,
            gated=False,
        )
    )
    await db_session.commit()

    # The "last tick" ran at 6:55pm ET, before tip -> resolves Preview and
    # persists that verdict onto event_desk_state.
    stale_tick_now = datetime(2026, 7, 10, 22, 55)
    await run_desk_tick(db_session, now=stale_tick_now, client=_fake_client())
    await db_session.commit()

    stale_state = await _event_desk_state_for(db_session)
    assert stale_state.daily_state == EventDailyState.PREVIEW

    # The request lands at 7:05pm ET -- 5 minutes after tip -- with NO new tick
    # having run. The game's status in the DB is still SCHEDULED.
    request_now = datetime(2026, 7, 10, 23, 5)
    payload = await get_desk_payload(db_session, now=request_now)

    assert payload is not None
    assert payload.daily_state == "live", (
        "expected the scheduled-tip fallback to render Live at request time "
        "even though the last tick's stored daily_state is still Preview"
    )

    # The stored event_desk_state row is untouched -- proves this call wrote
    # nothing and read the stale value only to demonstrate it disagrees.
    still_stale_state = await _event_desk_state_for(db_session)
    assert still_stale_state.daily_state == EventDailyState.PREVIEW


# --------------------------------------------------------------------------- #
# Preview (Morning Card)
# --------------------------------------------------------------------------- #
async def test_preview_state_assembles_hero_and_rest_of_slate_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Preview: hero from the #1 game, second (untriggered) game in `slate`; other sections empty."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)  # 4:00pm ET -- well before the 7pm tip.

    competition = await _seed_competition(db_session, year=year)
    home1, away1 = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    hero_game = await _seed_game(
        db_session,
        competition,
        home1,
        away1,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    home2, away2 = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    quiet_game = await _seed_game(
        db_session,
        competition,
        home2,
        away2,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 11, 1, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )

    player = await _seed_player(
        db_session, name="Rookie", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, home1, player)
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    await db_session.commit()

    result = await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()
    assert result.daily_state == EventDailyState.PREVIEW

    with count_queries(async_engine) as captured:
        payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "preview"
    assert payload.hero.kind == "marquee"
    assert payload.hero.game_id == hero_game.id
    assert payload.hero.subject_player_id == player.id

    # Section present/absent contract: only slate is relevant in Preview.
    slate_game_ids = {row.game_id for row in payload.slate}
    assert slate_game_ids == {quiet_game.id}
    assert payload.live_board == []
    assert payload.ledger == []

    # Class Tracker's fixed frame renders the rostered player by default.
    tracker_player_ids = {row.player_id for row in payload.tracker.rows}
    assert player.id in tracker_player_ids
    assert payload.tracker.cohort == "full_class"

    budget = DESK_HOME_QUERY_BUDGETS["preview"]
    assert len(captured) <= budget, (
        f"preview issued {len(captured)} queries, over budget of {budget}: {captured}"
    )


# --------------------------------------------------------------------------- #
# Live Desk
# --------------------------------------------------------------------------- #
async def test_live_state_populates_live_board_top_performer_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Live: hero is the in-progress game; live_board carries the top performer; slate/ledger empty."""
    year = 2026
    now = datetime(2026, 7, 10, 23, 30)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=40,
        away_score=38,
    )
    player = await _seed_player(
        db_session, name="Rookie", draft_year=year, draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_player,
        player=player,
        pts=24,
    )
    await db_session.commit()

    result = await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()
    assert result.daily_state == EventDailyState.LIVE

    with count_queries(async_engine) as captured:
        payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "live"
    assert payload.hero.kind == "live_duel"
    assert payload.hero.game_id == game.id

    assert len(payload.live_board) == 1
    board_row = payload.live_board[0]
    assert board_row.game_id == game.id
    assert board_row.top_performer_player_id == player.id
    assert board_row.top_performer_gmsc is not None and board_row.top_performer_gmsc > 0
    assert board_row.home_score == 40
    assert board_row.away_score == 38

    # #541 -- the (single-subject) Live hero's tonight running line matches
    # the exact box line seeded above, not the event-aggregate GmSc.
    assert payload.hero.subject_player_id == player.id
    assert payload.hero.subject_line is not None
    assert payload.hero.subject_line.pts == 24
    assert payload.hero.subject_line.reb == 8
    assert payload.hero.subject_line.ast == 5
    assert payload.hero.subject_line.gmsc == board_row.top_performer_gmsc
    # One-subject degradation (behavior spec): no second subject -> no second line.
    assert payload.hero.subject_player_id_2 is None
    assert payload.hero.subject_line_2 is None

    # Only one game today -> "games_today - 1 (hero)" leaves an empty slate.
    assert payload.slate == []
    assert payload.ledger == []

    budget = DESK_HOME_QUERY_BUDGETS["live"]
    assert len(captured) <= budget, (
        f"live issued {len(captured)} queries, over budget of {budget}: {captured}"
    )


async def test_live_state_duel_hero_renders_both_subjects_one_pretip_em_dash(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """#541: a Duel hero renders BOTH subjects' running lines; a not-yet-logged

    subject's line is a real `DeskHeroLine` with every field `None` (pretip
    em dash at render time), never a dropped line, a zero, or an event total.
    """
    year = 2026
    now = datetime(2026, 7, 10, 23, 30)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=40,
        away_score=38,
    )
    # Two top-14 prospects (Duel qualifies on draft slot alone -- no consensus
    # rank needed, `DUEL_PROMINENCE_CUTOFF`) sharing tonight's one game.
    player_a = await _seed_player(
        db_session, name="DuelA", draft_year=year, draft_round=1, draft_pick=1
    )
    player_b = await _seed_player(
        db_session, name="DuelB", draft_year=year, draft_round=1, draft_pick=2
    )
    source_a = await _roster_player(db_session, competition, home, player_a)
    await _roster_player(db_session, competition, away, player_b)
    await _seed_baseline(db_session, baseline_version="desk-home-v1")

    # Only player_a has logged tonight's game so far -- player_b hasn't
    # checked in yet on this tick's box score.
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_a,
        player=player_a,
        pts=22,
        reb=9,
        ast=6,
    )
    await db_session.commit()

    result = await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()
    assert result.daily_state == EventDailyState.LIVE

    with count_queries(async_engine) as captured:
        payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.hero.kind == "live_duel"
    assert {payload.hero.subject_player_id, payload.hero.subject_player_id_2} == {
        player_a.id,
        player_b.id,
    }

    lines_by_subject = {
        payload.hero.subject_player_id: payload.hero.subject_line,
        payload.hero.subject_player_id_2: payload.hero.subject_line_2,
    }
    logged_line = lines_by_subject[player_a.id]
    pretip_line = lines_by_subject[player_b.id]

    assert logged_line is not None
    assert (logged_line.pts, logged_line.reb, logged_line.ast) == (22, 9, 6)
    assert logged_line.gmsc is not None

    assert pretip_line is not None  # a real line object, not dropped
    assert (pretip_line.pts, pretip_line.reb, pretip_line.ast, pretip_line.gmsc) == (
        None,
        None,
        None,
        None,
    )

    budget = DESK_HOME_QUERY_BUDGETS["live"]
    assert len(captured) <= budget, (
        f"duel live issued {len(captured)} queries, over budget of {budget}: {captured}"
    )


# --------------------------------------------------------------------------- #
# The Ledger (Recap)
# --------------------------------------------------------------------------- #
async def test_recap_state_builds_ledger_from_last_final_within_budget(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Recap: ledger ranks last night's box lines by cohort percentile; slate/live_board empty."""
    year = 2026
    game_date = date(2026, 7, 10)
    # 11:00am ET the next morning -- after the final, before the next flip.
    now = datetime(2026, 7, 11, 15, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=game_date,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=88,
        away_score=80,
    )
    # Tonight's game (7pm ET / 23:00 UTC) keeps the outer lifecycle Active on
    # 7/11 and keeps `now` (11am ET) pre-flip, so Recap is the correct
    # request-time verdict rather than an off-window Wind-down gap.
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 11),
        tip_datetime=datetime(2026, 7, 11, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    player = await _seed_player(
        db_session, name="Rookie", draft_year=year, draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    # The Ledger (#539) ranks a single game's GmSc against the GAME-grain
    # baseline, not EVENT -- seed both so the percentile actually resolves.
    await _seed_baseline(
        db_session,
        baseline_version="desk-home-v1",
        cohort_key="game:1-4",
        grain=SummerLeagueDeskGrain.GAME,
    )
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_player,
        player=player,
        pts=30,
    )
    await db_session.commit()

    # Sync the events row (framework registration) without running the full
    # tick pipeline -- the Ledger assembly reads raw game logs + T1 directly,
    # not T2/T3/T4, so it doesn't need a tick to have run for `game_date`.
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    with count_queries(async_engine) as captured:
        payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "recap"
    assert payload.hero.kind == "performance_of_night"
    assert payload.hero.subject_player_id == player.id

    assert len(payload.ledger) == 1
    ledger_row = payload.ledger[0]
    assert ledger_row.player_id == player.id
    assert ledger_row.game_id == game.id
    assert ledger_row.gmsc > 0
    assert 0.0 <= ledger_row.pctl <= 100.0
    assert ledger_row.grade in {g.value for g in SummerLeagueDeskGrade}

    # Section present/absent: only ledger is relevant in Recap.
    assert payload.slate == []
    assert payload.live_board == []

    budget = DESK_HOME_QUERY_BUDGETS["recap"]
    assert len(captured) <= budget, (
        f"recap issued {len(captured)} queries, over budget of {budget}: {captured}"
    )


async def test_offday_recap_ledger_looks_back_to_last_nights_date(
    db_session: AsyncSession,
) -> None:
    """Off-day (behavior spec §2): the Ledger persists by finding the last FINAL date, not `today`."""
    year = 2026
    last_night = date(2026, 7, 10)
    future_game_date = date(2026, 7, 13)  # keeps the outer lifecycle Active.
    today = date(2026, 7, 11)  # zero games scheduled -- off-day.
    now = datetime(2026, 7, 11, 18, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=last_night,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.FINAL,
    )
    # A future game keeps the event's outer lifecycle window spanning today.
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=future_game_date,
        tip_datetime=datetime(2026, 7, 13, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    player = await _seed_player(
        db_session, name="Rookie", draft_year=year, draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    # The Ledger (#539) ranks a single game's GmSc against the GAME-grain
    # baseline, not EVENT -- seed both so the percentile actually resolves.
    await _seed_baseline(
        db_session,
        baseline_version="desk-home-v1",
        cohort_key="game:1-4",
        grain=SummerLeagueDeskGrain.GAME,
    )
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_player,
        player=player,
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "recap"
    assert len(payload.ledger) == 1
    assert payload.ledger[0].game_id == game.id


async def test_recap_ledger_renders_the_game_grain_percentile_not_event_grain(
    db_session: AsyncSession,
) -> None:
    """#539: a fixture where EVENT/GAME percentiles sharply diverge -- the Ledger renders GAME.

    Builds an EVENT-grain baseline whose breakpoints sit entirely ABOVE the
    subject's actual single-game GmSc (so ranking against it would clamp to
    the 0th percentile) and a GAME-grain baseline whose breakpoints sit
    entirely BELOW it (so ranking against it clamps to the 100th percentile).
    If `_assemble_ledger` were still reading the EVENT-grain row (the #539
    bug), this game would render as the worst possible percentile instead of
    the best.
    """
    year = 2026
    game_date = date(2026, 7, 10)
    now = datetime(2026, 7, 11, 15, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=game_date,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=88,
        away_score=80,
    )
    # Tonight's game keeps the outer lifecycle Active on 7/11 (Recap, not
    # Wind-down) -- same fixture shape as the sibling Recap tests above.
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 11),
        tip_datetime=datetime(2026, 7, 11, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    player = await _seed_player(
        db_session, name="Diverge", draft_year=year, draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_player,
        player=player,
        pts=30,
    )
    actual_gmsc = game_score_line(
        pts=30, fgm=8, fga=15, ftm=2, fta=2, oreb=1, dreb=7, ast=5, stl=1, blk=0, tov=2, pf=2
    )

    # EVENT grain: every breakpoint sits ABOVE the actual GmSc -- ranking
    # against this clamps to the 0th percentile.
    db_session.add(
        SummerLeagueCohortBaseline(
            baseline_version="desk-home-v1",
            is_active=True,
            cohort_key="slot:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.EVENT,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=40.0,
            n_members=20,
            breakpoints={"0": actual_gmsc + 100.0, "100": actual_gmsc + 200.0},
            mean_value=actual_gmsc + 150.0,
            median_value=actual_gmsc + 150.0,
        )
    )
    # GAME grain: every breakpoint sits BELOW the actual GmSc -- ranking
    # against this clamps to the 100th percentile.
    db_session.add(
        SummerLeagueCohortBaseline(
            baseline_version="desk-home-v1",
            is_active=True,
            cohort_key="game:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.GAME,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=10.0,
            n_members=20,
            breakpoints={"0": actual_gmsc - 200.0, "100": actual_gmsc - 100.0},
            mean_value=actual_gmsc - 150.0,
            median_value=actual_gmsc - 150.0,
        )
    )
    await db_session.commit()

    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "recap"
    assert len(payload.ledger) == 1
    ledger_row = payload.ledger[0]
    assert ledger_row.pctl == 100.0
    assert ledger_row.grade == "hot"

    # The hero ("Performance of the Night") reads off the same GAME-grain row.
    assert payload.hero.kind == "performance_of_night"
    assert payload.hero.subject_player_id == player.id
    assert "100th percentile" in (payload.hero.headline or "")


# --------------------------------------------------------------------------- #
# Quiet slate (behavior spec §4 -- never a dead hero)
# --------------------------------------------------------------------------- #
async def test_quiet_slate_hero_when_nothing_clears_the_threshold(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """A game today with no fired storyline (zero weight) still forces a headline.

    #544: a quiet (zero-weight) slate retains its schedule -- the one game
    tonight still shows in `payload.slate` even though the hero itself uses
    the class-leader fallback framing (`kind == "quiet_slate"`), rather than
    disappearing the way a pre-#544 quiet slate did.
    """
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    # No rostered tracked players at all -> no trigger can fire -> T4 weight 0.
    baseline_version = "desk-home-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)

    # A previously-graded class leader from an earlier tick/day, so the
    # fallback has someone to promote.
    leader = await _seed_player(
        db_session, name="Leader", draft_year=year, draft_round=1, draft_pick=2
    )
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=leader.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=80.0,
            pctl=95.0,
            grade=SummerLeagueDeskGrade.HOT,
            n_cohort=20,
            gated=False,
        )
    )
    await db_session.commit()

    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    with count_queries(async_engine) as captured:
        payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "preview"
    assert payload.hero.kind == "quiet_slate"
    assert payload.hero.subject_player_id == leader.id
    assert payload.hero.headline

    # #544: the one-game zero-signal night still lists in the slate -- a
    # quiet hero changes framing only, never drops the schedule.
    assert {row.game_id for row in payload.slate} == {game.id}

    budget = DESK_HOME_QUERY_BUDGETS["quiet_slate"]
    assert len(captured) <= budget, (
        f"quiet_slate issued {len(captured)} queries, over budget of {budget}: {captured}"
    )


async def test_quiet_slate_multi_game_night_retains_every_game(
    db_session: AsyncSession,
) -> None:
    """#544: a multi-game zero-signal night retains every game in `payload.slate`.

    Pre-#544, `slate_needs_quiet_fallback` short-circuited the whole games/
    teams/slate build, so a quiet multi-game night rendered an EMPTY "Rest of
    Tonight's Slate" even though several real games were scheduled. This
    proves all of them still show, unranked by any real signal (every row's
    `weight` is 0).
    """
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    competition = await _seed_competition(db_session, year=year)
    home1, away1 = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game1 = await _seed_game(
        db_session,
        competition,
        home1,
        away1,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    home2, away2 = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game2 = await _seed_game(
        db_session,
        competition,
        home2,
        away2,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 11, 1, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    # No rostered tracked players anywhere -> no trigger can fire on either
    # game -> both T4 rows carry zero weight.
    baseline_version = "desk-home-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    leader = await _seed_player(
        db_session, name="Leader", draft_year=year, draft_round=1, draft_pick=2
    )
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=leader.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=80.0,
            pctl=95.0,
            grade=SummerLeagueDeskGrade.HOT,
            n_cohort=20,
            gated=False,
        )
    )
    await db_session.commit()

    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.hero.kind == "quiet_slate"
    assert {row.game_id for row in payload.slate} == {game1.id, game2.id}
    assert all(row.weight <= 0 for row in payload.slate)


async def test_quiet_live_night_still_populates_live_board(
    db_session: AsyncSession,
) -> None:
    """#544: a quiet (zero-weight) LIVE night still populates the live board.

    Pre-#544 the live board was built inside the same "not quiet" branch as
    the hero/slate, so a zero-signal in-progress game rendered an EMPTY live
    board -- the Live Desk's whole "every game and its status" board
    disappeared, not just the hero. This proves the board still renders (em
    dashes for top performer since nobody tracked has logged anything).
    """
    year = 2026
    now = datetime(2026, 7, 10, 23, 30)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=10,
        away_score=8,
    )
    # No rostered tracked players -> no storyline weight, no top performer.
    baseline_version = "desk-home-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)
    leader = await _seed_player(
        db_session, name="Leader", draft_year=year, draft_round=1, draft_pick=2
    )
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=leader.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=80.0,
            pctl=95.0,
            grade=SummerLeagueDeskGrade.HOT,
            n_cohort=20,
            gated=False,
        )
    )
    await db_session.commit()

    result = await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()
    assert result.daily_state == EventDailyState.LIVE

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.daily_state == "live"
    assert payload.hero.kind == "quiet_slate"
    assert len(payload.live_board) == 1
    board_row = payload.live_board[0]
    assert board_row.game_id == game.id
    assert board_row.home_score == 10
    assert board_row.away_score == 8
    assert board_row.top_performer_player_id is None
    assert board_row.top_performer_gmsc is None


# --------------------------------------------------------------------------- #
# Class Tracker cohort filter
# --------------------------------------------------------------------------- #
async def test_tracker_cohort_filters_membership_and_falls_back_on_unknown(
    db_session: AsyncSession,
) -> None:
    """The undrafted cohort excludes a drafted player; an unknown cohort falls back to the default."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    drafted = await _seed_player(
        db_session, name="Drafted", draft_year=year, draft_round=1, draft_pick=5
    )
    undrafted = await _seed_player(
        db_session, name="Undrafted", draft_year=None, draft_round=None, draft_pick=None
    )
    await _roster_player(db_session, competition, home, drafted)
    await _roster_player(db_session, competition, away, undrafted)
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    await db_session.commit()

    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(
        db_session, now=now, tracker_cohort="undrafted", tracker_stat_view="box"
    )
    assert payload is not None
    tracker_ids = {row.player_id for row in payload.tracker.rows}
    assert undrafted.id in tracker_ids
    assert drafted.id not in tracker_ids
    undrafted_row = next(r for r in payload.tracker.rows if r.player_id == undrafted.id)
    assert undrafted_row.identity_label.startswith("Undrafted")

    # Unknown cohort/stat_view fall back to the documented defaults rather
    # than raising or silently rendering an empty board.
    fallback_payload = await get_desk_payload(
        db_session,
        now=now,
        tracker_cohort="not-a-real-cohort",
        tracker_stat_view="nope",
    )
    assert fallback_payload is not None
    assert fallback_payload.tracker.cohort == "full_class"
    assert fallback_payload.tracker.stat_view == "box"


# --------------------------------------------------------------------------- #
# Wind-down (#544: "final recap persists" through the post-roll tail)
# --------------------------------------------------------------------------- #
async def test_winddown_renders_last_ledger_instead_of_off_window(
    db_session: AsyncSession,
) -> None:
    """A day after the last final, still inside `post_roll_days`, renders the Ledger.

    Pre-#544, `_resolve_window_state` only recognized the ACTIVE lifecycle
    phase -- Wind-down (last final + `post_roll_days` tail) fell straight to
    the off-window archive strip, dropping the recap the morning after the
    event's last game. This proves Wind-down instead renders exactly the
    Recap treatment (`daily_state == "recap"`, last night's Ledger populated).
    """
    year = 2026
    last_game_date = date(2026, 7, 10)
    # One day after the only known game -- outside Active (no game today),
    # but inside the default `post_roll_days=2` Wind-down tail.
    today = date(2026, 7, 11)
    now = datetime(2026, 7, 11, 18, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=last_game_date,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=88,
        away_score=80,
    )
    player = await _seed_player(
        db_session, name="Rookie", draft_year=year, draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_baseline(
        db_session,
        baseline_version="desk-home-v1",
        cohort_key="game:1-4",
        grain=SummerLeagueDeskGrain.GAME,
    )
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_player,
        player=player,
        pts=30,
    )
    await db_session.commit()

    # No future game seeded -- the calendar's only cluster is [7/10, 7/10],
    # so `today` (7/11) resolves to Wind-down, not Active.
    await sync_summer_league_event(db_session, today)
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None, (
        "Wind-down must still take over the home page with the last recap, "
        "not collapse to the off-window archive strip"
    )
    assert payload.daily_state == "recap"
    assert payload.is_home_owner is True
    assert len(payload.ledger) == 1
    assert payload.ledger[0].game_id == game.id
    assert payload.hero.kind == "performance_of_night"

    # Class Tracker still renders during Wind-down (it's pinned, not gated by
    # daily_state).
    tracker_ids = {row.player_id for row in payload.tracker.rows}
    assert player.id in tracker_ids


async def test_archived_after_post_roll_tail_stays_off_window(
    db_session: AsyncSession,
) -> None:
    """Once the post-roll tail elapses (Archived), the Desk goes back off-window."""
    year = 2026
    last_game_date = date(2026, 7, 10)
    # Default `post_roll_days=2` -> Wind-down ends 7/12; 7/13 is Archived.
    today = date(2026, 7, 13)
    now = datetime(2026, 7, 13, 18, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=last_game_date,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.FINAL,
    )
    await db_session.commit()

    await sync_summer_league_event(db_session, today)
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)
    assert payload is None


# --------------------------------------------------------------------------- #
# Ownership + force mode (#544)
# --------------------------------------------------------------------------- #
async def test_auto_mode_does_not_take_over_when_not_home_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto mode requires BOTH an active window AND `is_home_owner`.

    Losing ownership (e.g. a future higher-priority event winning the
    tie-break) must render off-window, not silently take over anyway.
    """
    year = 2026
    now = datetime(2026, 7, 10, 23, 30)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.summer_league.desk_read.resolve_home_owner",
        lambda now, events: None,  # nobody wins the takeover this tick.
    )

    payload = await get_desk_payload(db_session, now=now)
    assert payload is None


async def test_force_on_bypasses_ownership_gate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sl_desk_force_mode="on"` still takes over even when not the resolved owner."""
    year = 2026
    now = datetime(2026, 7, 10, 23, 30)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.summer_league.desk_read.resolve_home_owner",
        lambda now, events: None,
    )
    monkeypatch.setattr(settings, "sl_desk_force_mode", "on")

    payload = await get_desk_payload(db_session, now=now)
    assert payload is not None
    assert payload.daily_state == "live"


async def test_force_off_never_takes_over_even_when_active_and_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sl_desk_force_mode="off"` is a kill switch -- off regardless of the calendar."""
    year = 2026
    now = datetime(2026, 7, 10, 23, 30)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    # Sanity: this event is genuinely active + owner without the override.
    baseline_payload = await get_desk_payload(db_session, now=now)
    assert baseline_payload is not None

    monkeypatch.setattr(settings, "sl_desk_force_mode", "off")
    payload = await get_desk_payload(db_session, now=now)
    assert payload is None


async def test_force_date_override_pins_the_resolved_calendar_date(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sl_desk_force_date` overrides which date the Desk resolves against.

    Even though the request instant passed to `get_desk_payload` is an
    otherwise off-window date (year 2099), the forced date lands squarely on
    a real in-progress game -- proving the override wins over the caller's
    own `now`, the same way an operator's env var would in production.
    """
    year = 2026
    game_date = date(2026, 7, 15)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=game_date,
        tip_datetime=datetime(2026, 7, 15, 23, 0),
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, game_date)
    await db_session.commit()

    monkeypatch.setattr(settings, "sl_desk_force_date", game_date)
    # The passed `now` is a wildly off-window YEAR, but the same time-of-day
    # (23:30) as the seeded tip -- only the date should come from the override.
    off_window_now = datetime(2099, 1, 1, 23, 30)

    payload = await get_desk_payload(db_session, now=off_window_now)
    assert payload is not None
    assert payload.daily_state == "live"


# --------------------------------------------------------------------------- #
# Freshness (#544: missing state never says "as of now"; stale after cadence)
# --------------------------------------------------------------------------- #
async def test_freshness_missing_before_any_tick_has_run(
    db_session: AsyncSession,
) -> None:
    """No `event_desk_state` row yet -> "missing", never a fabricated "as of now"."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    await db_session.commit()

    # Registers the `events` row WITHOUT running the tick pipeline -- no
    # `event_desk_state` row exists yet, matching "the tick has never run".
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.freshness.state == "missing"
    assert payload.freshness.last_tick_at is None
    assert payload.freshness.next_tick_eta_label is None
    assert "unavailable" in payload.freshness.as_of_et_label
    assert "as of" not in payload.freshness.as_of_et_label


async def test_freshness_fresh_immediately_after_a_tick(
    db_session: AsyncSession,
) -> None:
    """A tick that just ran renders "fresh" with a next-update ETA."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    await db_session.commit()

    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)

    assert payload is not None
    assert payload.freshness.state == "fresh"
    assert "stale" not in payload.freshness.as_of_et_label
    assert payload.freshness.next_tick_eta_label is not None
    assert "next update" in payload.freshness.next_tick_eta_label


async def test_freshness_stale_after_the_documented_cadence_multiple(
    db_session: AsyncSession,
) -> None:
    """A tick 3 hours old (> the 2x hourly-cadence threshold) renders "stale"."""
    year = 2026
    request_now = datetime(2026, 7, 10, 20, 0)
    tick_now = request_now - timedelta(hours=3)

    competition = await _seed_competition(db_session, year=year)
    home, away = (
        await _seed_team(db_session, competition),
        await _seed_team(db_session, competition),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    await _seed_baseline(db_session, baseline_version="desk-home-v1")
    await db_session.commit()

    await run_desk_tick(db_session, now=tick_now, client=_fake_client())
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=request_now)

    assert payload is not None
    assert payload.freshness.state == "stale"
    assert payload.freshness.as_of_et_label.endswith("-- stale")
    assert payload.freshness.last_tick_at == tick_now

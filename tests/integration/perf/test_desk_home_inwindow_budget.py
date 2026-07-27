"""In-window `/` query-budget guard for the Summer League Desk (#508 follow-up).

`test_route_query_budgets.py` measures `/` against `representative_dataset`,
which seeds no `events`/T1-T4 rows -- so it only ever exercises the Desk's
**off-window** short-circuit (one `events` lookup, then `None`). During the SL
event itself (Vegas 2026: Jul 9-19), `/` is *in-window*: it renders the full
Desk and fires ~an-order-more queries. That composite render -- the exact
configuration live during the event -- had zero budget protection until this
file.

This test layers an **active** SL event on top of the full representative home
dataset and asserts the composite `/` query count per Desk state, so a future
regression that bolts a per-player/per-game query onto the Desk fails CI instead
of shipping mid-event. Budgets live in
`tests/integration/perf/budgets.py::DESK_HOME_PAGE_BUDGETS` (the full-page,
in-window numbers -- distinct from `DESK_HOME_QUERY_BUDGETS`, which budgets the
`get_desk_payload` service call in isolation).

The Desk resolves its state from the *real* request clock (`app.routes.ui.home`
calls `get_desk_payload(now=<now>)` with the current instant -- the route can't
be handed a fake `now`), so the fixture seeds today's (Eastern-date) games with
a status that forces the target state regardless of the wall clock: an
``in_progress`` game -> Live (Live always wins), an all-``final`` slate -> Recap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.event_desk.registry import sync_summer_league_event
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.cli.sl_desk_tick import run_desk_tick
from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import DESK_HOME_PAGE_BUDGETS
from tests.integration.perf.conftest import SeededData

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    """Minimal curl_cffi-shaped response returning a fixed JSON payload."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200

    def json(self) -> object:
        """Return the configured payload."""
        return self.payload


class _FakeSession:
    """Fake NBA Stats session that never hits the network (empty schedule)."""

    def get(self, url: str, params: dict[str, str]) -> _FakeResponse:
        """Return an empty schedule -- games are seeded directly by the fixture."""
        return _FakeResponse({"leagueSchedule": {"gameDates": []}})

    def close(self) -> None:
        """No-op close (matches the real session interface)."""


_IDX = {"n": 0}


def _idx() -> int:
    _IDX["n"] += 1
    return _IDX["n"]


async def _seed_active_desk(
    db: AsyncSession, *, now: datetime, game_status: SummerLeagueGameStatus
) -> None:
    """Seed an active SL event whose today-dated slate forces one Desk state.

    Populates the full projection stack the way production does -- competition,
    an `events` registry row (via `sync_summer_league_event`), teams, two
    today-dated games with `tip_datetime`, a rostered/graded player with a
    season aggregate + game log, and an active T1 baseline -- then runs the real
    `run_desk_tick` (fake empty-schedule client) so T2/T3/T4/`event_desk_state`
    are written exactly as the hourly cron writes them.

    Args:
        db: Active database session (committed by the caller).
        now: The reference instant used both to date the slate (its Eastern
            date) and to run the tick; must be the same wall-clock day the
            request under test lands on.
        game_status: The status stamped on both of today's games -- picks the
            resolved state (`IN_PROGRESS` -> Live, `FINAL` -> Recap).
    """
    today = to_eastern_date(now)
    year = today.year

    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug=f"vegas-inwindow-{_idx()}",
        display_name=f"{year} Las Vegas",
        starts_on=today - timedelta(days=2),
        ends_on=today + timedelta(days=8),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None

    teams: list[SummerLeagueTeamEntry] = []
    for _ in range(4):
        i = _idx()
        team = SummerLeagueTeamEntry(
            competition_id=comp.id,
            nba_stats_team_id=f"inwin-team-{i}",
            raw_team_name=f"Team {i}",
            raw_team_abbreviation=f"T{i}",
            team_slug=f"inwin-team-{i}",
        )
        db.add(team)
        teams.append(team)
    await db.flush()

    games: list[SummerLeagueGame] = []
    for home, away in ((teams[0], teams[1]), (teams[2], teams[3])):
        assert home.id is not None and away.id is not None
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"inwin-game-{_idx()}",
            game_date=today,
            tip_datetime=now - timedelta(hours=1),
            home_team_entry_id=home.id,
            away_team_entry_id=away.id,
            home_score=55 if game_status != SummerLeagueGameStatus.SCHEDULED else None,
            away_score=50 if game_status != SummerLeagueGameStatus.SCHEDULED else None,
            status=game_status,
        )
        db.add(game)
        games.append(game)
    await db.flush()

    # A rostered, lottery-slot player on the hero game's home team, with a
    # season aggregate (so the tick can grade them) and a game log (so the Live
    # board's top-performer + the Ledger have a resolved line to read).
    player = PlayerMaster(
        first_name="Marquee",
        last_name=f"Rookie{_idx()}",
        display_name=f"Marquee Rookie {_idx()}",
        draft_year=year,
        draft_round=1,
        draft_pick=1,
        position="G",
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None

    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"inwin-person-{_idx()}",
        raw_player_name=player.display_name or "Marquee Rookie",
        normalized_name=(player.display_name or "marquee rookie").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None

    db.add(
        SummerLeagueParticipation(
            competition_id=comp.id,
            team_entry_id=teams[0].id,
            source_player_id=source_player.id,
            player_id=player.id,
            roster_status=AffiliationStatus.ACTIVE,
        )
    )
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=comp.id,
            player_id=player.id,
            year=year,
            venue_slug=comp.venue_slug,
            gp=3,
            minutes=90.0,
            gmsc=72.0,
        )
    )
    assert games[0].id is not None
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=games[0].id,
            team_entry_id=teams[0].id,
            source_player_id=source_player.id,
            player_id=player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=player.display_name or "Marquee Rookie",
            minutes_seconds=1800,
            pts=26,
            fgm=9,
            fga=16,
            ftm=4,
            fta=4,
            oreb=1,
            dreb=6,
            reb=7,
            ast=5,
            stl=2,
            blk=1,
            tov=2,
            pf=2,
        )
    )
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version="inwindow-v1",
            is_active=True,
            cohort_key="slot:1-4",
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
    )
    await db.flush()

    # Register the events row + populate T2/T3/T4/event_desk_state via the real
    # tick (empty-schedule fake client -> no network, no game overwrites).
    await sync_summer_league_event(db, today)
    await run_desk_tick(db, now=now, client=NBAStatsClient(session=_FakeSession()))


@dataclass(frozen=True)
class _DeskStateCase:
    """One in-window `/` measurement case: its Desk state + seed status."""

    state: str
    game_status: SummerLeagueGameStatus


_CASES: tuple[_DeskStateCase, ...] = (
    _DeskStateCase("live", SummerLeagueGameStatus.IN_PROGRESS),
    _DeskStateCase("recap", SummerLeagueGameStatus.FINAL),
)


@pytest_asyncio.fixture(params=_CASES, ids=lambda c: c.state)
async def in_window_dataset(
    request: pytest.FixtureRequest,
    representative_dataset: SeededData,
    db_session: AsyncSession,
) -> _DeskStateCase:
    """Full home dataset + an active SL Desk in the parametrized state."""
    case: _DeskStateCase = request.param
    await _seed_active_desk(
        db_session, now=datetime.utcnow(), game_status=case.game_status
    )
    await db_session.commit()
    return case


async def test_in_window_home_within_budget(
    in_window_dataset: _DeskStateCase,
    app_client: AsyncClient,
    async_engine: AsyncEngine,
) -> None:
    """`/` renders the full in-window Desk within its committed per-state budget.

    A failure means the Desk (or its home wiring) added queries to the
    in-window `/` render. Fix an accidental N+1/serial load, or -- if the query
    is genuinely required -- raise `DESK_HOME_PAGE_BUDGETS[state]` in this same
    diff so the added per-request cost is reviewed.
    """
    case = in_window_dataset
    budget = DESK_HOME_PAGE_BUDGETS[case.state]

    # Warm up once untracked so one-time process caches (e.g. the school-logo
    # map) don't count, mirroring test_route_query_budgets.py.
    warmup = await app_client.get("/")
    assert warmup.status_code == 200, (
        f"/ returned {warmup.status_code} on the warm-up render ({case.state})."
    )

    with count_queries(async_engine) as captured:
        response = await app_client.get("/")

    assert response.status_code == 200, (
        f"/ returned {response.status_code} in-window ({case.state}); expected 200."
    )

    # Guard against a silent collapse to the off-window strip: if a
    # materialization regression dropped the in-window Desk, the query count
    # would fall *under* budget and this test would otherwise pass green. Assert
    # the full Desk section actually rendered (and the off-window fallback did
    # not) so the budget check can only pass on a genuinely in-window render.
    assert 'id="slDeskSection"' in response.text, (
        f"in-window / ({case.state}) did not render the full Desk section "
        f"(id=slDeskSection missing); a within-budget pass here would be a false "
        f"positive from a silently collapsed render."
    )
    assert "desk__offwindow" not in response.text, (
        f"in-window / ({case.state}) rendered the off-window strip; the Desk "
        f"collapsed to the dormant fallback while nominally in-window."
    )

    if len(captured) > budget:
        listing = "\n".join(
            f"  {i + 1:>2}. {' '.join(stmt.split())[:120]}"
            for i, stmt in enumerate(captured)
        )
        pytest.fail(
            f"in-window / ({case.state}) issued {len(captured)} queries, over its "
            f"budget of {budget}.\nIf this is an accidental N+1 / extra serial "
            f"query in the Desk, fix it. If genuinely needed, raise "
            f"DESK_HOME_PAGE_BUDGETS[{case.state!r}] in "
            f"tests/integration/perf/budgets.py in this same diff.\n"
            f"Captured statements:\n{listing}"
        )

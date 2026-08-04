"""Roster-size query-growth guard for the Summer League Desk hourly tick (#548).

Before #548, three of the tick's steps grew their query count linearly with
roster size: Job B step 2 (`desk_grades.grade_player_event`, 5 queries PER
PLAYER: competition fetch, player fetch, season-rows select, baseline
select, upsert), step 3's per-slot storyline context (`desk_storylines`'s
now-removed `_game_lines_before`/`_prior_event`/`_current_event_gp`, up to 3
queries per graded slot), and step 5's commentary persistence
(`desk_commentary.persist_grade_facts`, a select + flush PER PLAYER). #548
replaced all three with batched, `.in_()`-scoped fetches and bulk
upserts/updates (`grade_players_bulk`, the batched context fetches in
`compute_desk_storylines`, `persist_grade_facts_bulk`/`persist_slate_facts_bulk`)
so none of these steps' query count scales with roster size any more.

This test proves the fix behaviorally rather than by code inspection: run one
tick over a 2-player roster and a separate one over a 20-player roster (same
shape otherwise -- one game, one shared cohort, one baseline) and assert the
20-player tick's query count is within a small, roster-size-INDEPENDENT delta
of the 2-player tick's. Isolation between the two scenarios comes from using
different competition years (`resolve_target_competitions` scopes to
`today.year`), not separate database sessions, so this stays a single fast
test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.cli.sl_desk_tick import run_desk_tick
from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import DESK_TICK_DURATION_BUDGET_MS

pytestmark = pytest.mark.asyncio

_IDX = {"n": 0}


def _idx() -> int:
    _IDX["n"] += 1
    return _IDX["n"]


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


async def _seed_roster_scenario(
    db: AsyncSession, *, year: int, n_players: int, now: datetime
) -> None:
    """Seed one competition, one FINAL game today, and ``n_players`` graded rostered players.

    Every player shares the SAME cohort (``slot:1-4``, round 1 picks 1-4
    cycled) so exactly one T1 baseline row covers the whole roster -- the
    scenario varies ONLY in roster size, isolating that as the query-count
    growth variable this test measures.
    """
    today = to_eastern_date(now)
    comp = SummerLeagueEdition(
        year=year,
        league_id="15",
        venue_slug=f"growth-{_idx()}",
        display_name=f"{year} Query Growth Fixture",
        starts_on=today - timedelta(days=2),
        ends_on=today + timedelta(days=8),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None

    home = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"growth-team-{_idx()}",
        raw_team_name="Home",
        team_slug=f"growth-home-{_idx()}",
    )
    away = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"growth-team-{_idx()}",
        raw_team_name="Away",
        team_slug=f"growth-away-{_idx()}",
    )
    db.add(home)
    db.add(away)
    await db.flush()
    assert home.id is not None and away.id is not None

    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"growth-game-{_idx()}",
        game_date=today,
        tip_datetime=now - timedelta(hours=3),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=SummerLeagueGameStatus.FINAL,
    )
    db.add(game)
    await db.flush()

    for i in range(n_players):
        team = home if i % 2 == 0 else away
        player = PlayerMaster(
            first_name=f"Grow{i}",
            last_name=f"Player{_idx()}",
            display_name=f"Grow{i} Player {_idx()}",
            draft_year=year,
            draft_round=1,
            draft_pick=(i % 4) + 1,
            position="G",
            is_stub=False,
        )
        db.add(player)
        await db.flush()
        assert player.id is not None

        source_player = SummerLeagueSourceRecord(
            nba_stats_person_id=f"growth-src-{_idx()}",
            raw_player_name=player.display_name or "",
            normalized_name=(player.display_name or "").lower(),
            canonical_player_id=player.id,
        )
        db.add(source_player)
        await db.flush()
        assert source_player.id is not None

        db.add(
            SummerLeagueParticipation(
                competition_id=comp.id,
                team_entry_id=team.id,
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
                is_current=True,
                gp=4,
                minutes=120.0,
                gmsc=45.0 + i,
            )
        )
    # The active-baseline guard (#756) intentionally permits only one
    # published row per cohort.  This fixture runs both roster scenarios in
    # one session, so model the newer scenario superseding the prior
    # publication before inserting its replacement rows.
    await db.execute(
        update(SummerLeagueCohortBaseline)
        .where(SummerLeagueCohortBaseline.is_active.is_(True))  # type: ignore[attr-defined]
        .where(
            SummerLeagueCohortBaseline.cohort_key.in_(  # type: ignore[attr-defined]
                ("slot:1-4", "game:1-4")
            )
        )
        .values(is_active=False)
    )
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=f"growth-v{year}",
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
    # Game-grain baseline too -- the streak trigger's context fetch reads it.
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=f"growth-v{year}",
            is_active=True,
            cohort_key="game:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.GAME,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=0.0,
            n_members=20,
            breakpoints={"0": 5.0, "25": 15.0, "50": 25.0, "75": 35.0, "100": 45.0},
            mean_value=25.0,
            median_value=25.0,
        )
    )
    await db.flush()
    await db.commit()


async def _run_tick_and_count(
    db: AsyncSession, async_engine: AsyncEngine, *, now: datetime, tmp_path: Path
) -> tuple[int, float]:
    """Run one tick, returning its query count and wall-clock duration (ms)."""
    client = NBAStatsClient(session=_FakeSession())
    started_at = perf_counter()
    with count_queries(async_engine) as captured:
        await run_desk_tick(db, now=now, raw_root=tmp_path, client=client)
    duration_ms = (perf_counter() - started_at) * 1000
    await db.commit()
    return len(captured), duration_ms


# A generous, roster-size-INDEPENDENT ceiling on the 20-vs-2-player delta.
# Measured post-#548: the 20-player tick issues the SAME query count as the
# 2-player one (delta=0) -- every step batches via `.in_(...)` over however
# many players/slots are in play rather than issuing one statement per
# player, so the roster-size axis costs nothing extra query-count-wise.
# `_MAX_GROWTH_DELTA` leaves a little headroom above that measured 0 (e.g. a
# future genuinely-necessary per-tick addition unrelated to roster size)
# rather than asserting exact equality. Under the pre-#548 per-player loops
# this delta would have been on the order of 18 extra players x ~8
# queries/player (grade + storyline context + fact persist) -- 100+ -- so
# this bound is not a coincidence: it is what "O(1), not O(n), roster-size
# query growth" looks like as an assertion.
_MAX_GROWTH_DELTA = 3


async def test_tick_query_count_growth_bounded_not_linear_in_roster_size(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """20 rostered players cost roughly the same tick query count as 2 (#548).

    Two independent scenarios (different competition years, so
    `resolve_target_competitions`'s year scoping keeps them from mixing in
    one `run_desk_tick` call) sharing one DB session: a 2-player roster and a
    20-player roster, otherwise identical (one FINAL game, one shared
    cohort/baseline). If any step in the tick still grew its query count
    per-player, the 20-player delta would dwarf `_MAX_GROWTH_DELTA`.
    """
    now_a = datetime(2026, 7, 10, 22, 0)
    await _seed_roster_scenario(db_session, year=2026, n_players=2, now=now_a)
    count_2, duration_2_ms = await _run_tick_and_count(
        db_session, async_engine, now=now_a, tmp_path=tmp_path
    )

    now_b = datetime(2027, 7, 10, 22, 0)
    await _seed_roster_scenario(db_session, year=2027, n_players=20, now=now_b)
    count_20, duration_20_ms = await _run_tick_and_count(
        db_session, async_engine, now=now_b, tmp_path=tmp_path
    )

    delta = count_20 - count_2
    assert delta <= _MAX_GROWTH_DELTA, (
        f"20-player tick issued {count_20} queries vs {count_2} for 2 players "
        f"(delta={delta}), over the roster-size-independent bound of "
        f"{_MAX_GROWTH_DELTA}. A per-player/per-slot query growth regression "
        "likely reappeared in grade_players_bulk / compute_desk_storylines / "
        "persist_grade_facts_bulk / persist_slate_facts_bulk."
    )

    # Desk-tick wall-clock budget (#629, project's two-minute Desk-tick
    # target -- see DESK_TICK_DURATION_BUDGET_MS's docstring in budgets.py).
    # This synthetic/no-network fixture should finish orders of magnitude
    # under that ceiling; a regression that reintroduces real per-row work
    # would show up here as wall-clock cost even if the query-count guard
    # above somehow didn't catch it.
    assert duration_2_ms <= DESK_TICK_DURATION_BUDGET_MS, (
        f"2-player Desk tick took {duration_2_ms:.0f}ms, over the "
        f"{DESK_TICK_DURATION_BUDGET_MS}ms budget"
    )
    assert duration_20_ms <= DESK_TICK_DURATION_BUDGET_MS, (
        f"20-player Desk tick took {duration_20_ms:.0f}ms, over the "
        f"{DESK_TICK_DURATION_BUDGET_MS}ms budget"
    )

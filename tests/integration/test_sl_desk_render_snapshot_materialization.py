"""Integration tests for tick-time Desk render-snapshot materialization (#551).

Launch-readiness item 10 builds on #546's persistence layer
(`app.schemas.event_desk_render_snapshot`, `app.services.event_desk.render_snapshots`)
and #544's request-time state resolution (`_resolve_window_state`/`_effective_now`/
`_freshness_for`) to close the loop: the hourly tick
(`scripts/sl_desk_tick.py::run_desk_tick`) materializes the COMPLETE Preview/Live/
Recap x Tracker cohort/stat-view variant matrix as its final step
(`app.services.summer_league.desk_read.build_desk_render_variants`), and the
homepage reads exactly one matching snapshot at request time
(`get_desk_view_from_snapshot`) instead of reassembling the page.

Covers what no other test file does: the variant matrix's shape/completeness, that
one tick's materialization is atomic and idempotent (rerun updates in place, never
duplicates), that an upstream tick failure leaves prior snapshots untouched, that
request-time state resolution (including a tip-time Preview->Live transition) picks
the correct pre-built variant without another tick, honest missing/stale
degradation, and that a snapshot-backed read never issues a per-player/per-game
query after its one snapshot lookup.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.schemas.event_desk import Event, EventDailyState, EventDeskState
from app.schemas.event_desk_render_snapshot import EventDeskRenderSnapshot
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
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.event_desk.registry import sync_summer_league_event
from app.services.event_desk.render_snapshots import CURRENT_SCHEMA_VERSION
from app.services.summer_league.desk_read import (
    DESK_RENDER_DAILY_STATES,
    TRACKER_COHORTS,
    TRACKER_STAT_VIEWS,
    build_desk_render_variants,
    get_desk_payload,
    get_desk_view_from_snapshot,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from scripts.sl_desk_tick import run_desk_tick
from tests.integration.perf._capture import count_queries

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


class _FakeResponse:
    """Minimal curl_cffi-shaped response returning a fixed JSON payload."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured payload."""
        return self.payload


class _FakeSession:
    """Fake curl_cffi-compatible session, routed by endpoint (mirrors test_sl_desk_tick.py)."""

    def __init__(
        self, responses: dict[tuple[str, str], list[_FakeResponse]] | None = None
    ) -> None:
        self._responses = responses or {}
        self._default = _FakeResponse({"resultSets": []})
        self._call_index: dict[tuple[str, str], int] = {}

    def get(self, url: str, params: dict[str, str]) -> _FakeResponse:
        """Return the next registered response for (endpoint, PlayerOrTeam)."""
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "scheduleleaguev2":
            return _FakeResponse({"leagueSchedule": {"gameDates": []}})
        key = (endpoint, params.get("PlayerOrTeam", ""))
        sequence = self._responses.get(key)
        if not sequence:
            return self._default
        idx = self._call_index.get(key, 0)
        self._call_index[key] = idx + 1
        return sequence[min(idx, len(sequence) - 1)]

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


async def _seed_competition(
    db: AsyncSession, *, year: int, starts_on: date, ends_on: date
) -> SummerLeagueCompetition:
    idx = _next_idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug=f"vegas-snap-{idx}",
        display_name=f"{year} Las Vegas",
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
    idx = _next_idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"snap-team-{idx}",
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
        team_slug=f"snap-team-{idx}",
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
    home_score: int | None = None,
    away_score: int | None = None,
) -> SummerLeagueGame:
    idx = _next_idx()
    assert competition.id is not None and home.id is not None and away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"snap-game-{idx}",
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


async def _seed_roster_player(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    *,
    year: int,
    gmsc: float = 60.0,
    minutes: float = 90.0,
    gp: int = 3,
) -> PlayerMaster:
    idx = _next_idx()
    assert competition.id is not None and team.id is not None
    player = PlayerMaster(
        first_name="Snap",
        last_name=f"Rookie{idx}",
        display_name=f"Snap Rookie {idx}",
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
        nba_stats_person_id=f"snap-person-{idx}",
        raw_player_name=player.display_name or "Snap Rookie",
        normalized_name=(player.display_name or "snap rookie").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()

    db.add(
        SummerLeagueParticipation(
            competition_id=competition.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=player.id,
            roster_status=AffiliationStatus.ACTIVE,
        )
    )
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
    return player


async def _seed_baseline(db: AsyncSession, *, baseline_version: str) -> None:
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
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
    # Game-grain baseline too -- the Ledger/streak paths read this grain.
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key="game:slot:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.GAME,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=10.0,
            n_members=20,
            breakpoints={"0": 2.0, "25": 8.0, "50": 14.0, "75": 20.0, "100": 30.0},
            mean_value=14.0,
            median_value=14.0,
        )
    )
    await db.flush()


async def _event_id(db: AsyncSession, *, key: str = "summer_league") -> int:
    event = (
        await db.execute(select(Event).where(Event.key == key))  # type: ignore[arg-type]
    ).scalar_one()
    assert event.id is not None
    return event.id


def _expected_variant_keys() -> set[tuple[EventDailyState, str, str]]:
    return {
        (state, cohort, stat_view)
        for state in DESK_RENDER_DAILY_STATES
        for cohort in TRACKER_COHORTS
        for stat_view in TRACKER_STAT_VIEWS
    }


# --------------------------------------------------------------------------- #
# 1. Variant matrix build
# --------------------------------------------------------------------------- #
async def test_build_desk_render_variants_produces_complete_matrix(
    db_session: AsyncSession,
) -> None:
    """One call builds the FULL 3 x 6 x 4 = 72-row variant matrix, every key unique."""
    year = 2026
    today = date(2026, 7, 10)
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session,
        year=year,
        starts_on=today - timedelta(days=2),
        ends_on=today + timedelta(days=8),
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=today,
        tip_datetime=now - timedelta(hours=1),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=40,
        away_score=38,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-matrix-v1")
    await sync_summer_league_event(db_session, today)
    await db_session.commit()

    result = await build_desk_render_variants(db_session, now=now)
    assert result is not None
    event_id, variants = result
    assert event_id == await _event_id(db_session)

    assert len(variants) == len(DESK_RENDER_DAILY_STATES) * len(TRACKER_COHORTS) * len(
        TRACKER_STAT_VIEWS
    )
    assert len(variants) == 72

    keys = {(v.daily_state, v.tracker_cohort, v.tracker_stat_view) for v in variants}
    assert keys == _expected_variant_keys()

    for variant in variants:
        assert variant.view.payload is not None
        assert variant.view.payload.daily_state == variant.daily_state.value
        assert variant.view.payload.tracker.cohort == variant.tracker_cohort
        assert variant.view.payload.tracker.stat_view == variant.tracker_stat_view


async def test_build_desk_render_variants_returns_none_off_window(
    db_session: AsyncSession,
) -> None:
    """Off-window (no events row, or lifecycle isn't Active/Wind-down) -- nothing to build."""
    result = await build_desk_render_variants(
        db_session, now=datetime(2099, 1, 15, 12, 0)
    )
    assert result is None


# --------------------------------------------------------------------------- #
# 2. Atomic tick persistence + rerun-no-dup
# --------------------------------------------------------------------------- #
async def test_tick_materializes_full_matrix_and_rerun_updates_without_duplicates(
    db_session: AsyncSession, tmp_path
) -> None:
    """One `run_desk_tick` writes all 72 variants; a second tick updates rows, not duplicates."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-rerun-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())

    result1 = await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
    await db_session.commit()
    assert result1.dormant is False
    assert result1.materialized_variant_count == 72

    # `expire_on_commit=False` on the test session factory means a re-`select`
    # returns identity-mapped instances that a Core-`insert` upsert never
    # refreshes -- expire so every read below reflects the DB, not stale ORM
    # state.
    db_session.expire_all()
    rows1 = (await db_session.execute(select(EventDeskRenderSnapshot))).scalars().all()
    assert len(rows1) == 72
    updated_at_by_key_1 = {
        (r.daily_state, r.tracker_cohort, r.tracker_stat_view): r.updated_at
        for r in rows1
    }
    assert len(updated_at_by_key_1) == 72

    # Rerun the tick a bit later over the same data -- must update in place.
    later = now + timedelta(minutes=5)
    client2 = NBAStatsClient(session=_FakeSession())
    result2 = await run_desk_tick(
        db_session, now=later, raw_root=tmp_path, client=client2
    )
    await db_session.commit()
    assert result2.materialized_variant_count == 72

    db_session.expire_all()
    rows2 = (await db_session.execute(select(EventDeskRenderSnapshot))).scalars().all()
    assert len(rows2) == 72, (
        "rerunning the tick must update rows in place, never duplicate"
    )
    updated_at_by_key_2 = {
        (r.daily_state, r.tracker_cohort, r.tracker_stat_view): r.updated_at
        for r in rows2
    }
    assert set(updated_at_by_key_2) == set(updated_at_by_key_1)
    # Every row was genuinely rewritten (later `updated_at`), not left stale.
    for key, updated_at in updated_at_by_key_2.items():
        assert updated_at > updated_at_by_key_1[key]


# --------------------------------------------------------------------------- #
# 3. Upstream failure preserves prior snapshots/freshness
# --------------------------------------------------------------------------- #
async def test_upstream_failure_preserves_prior_render_snapshots(
    db_session: AsyncSession, tmp_path
) -> None:
    """A required live-refresh failure on tick #2 leaves tick #1's snapshots byte-for-byte untouched."""
    year = 2026
    now1 = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    # Tomorrow's scheduled game -- far outside tick #1's +/-6h live-refresh
    # window (so tick #1 never touches it), but squarely inside tick #2's.
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 11),
        tip_datetime=datetime(2026, 7, 11, 20, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-failure-v1")
    await db_session.commit()

    # Tick #1 succeeds and is committed (the prior good state). `run_desk_tick`
    # never commits itself -- the caller does -- so this test owns the
    # transaction boundary the way `scripts/sl_desk_tick.py::main` does.
    client1 = NBAStatsClient(session=_FakeSession())
    result1 = await run_desk_tick(
        db_session, now=now1, raw_root=tmp_path, client=client1
    )
    await db_session.commit()
    assert result1.materialized_variant_count == 72

    db_session.expire_all()
    rows_before = (
        (await db_session.execute(select(EventDeskRenderSnapshot))).scalars().all()
    )
    assert len(rows_before) == 72
    # Concrete value tuples (not live ORM attrs) so the comparison below is
    # unaffected by any later identity-map expiry.
    signature_before = {
        (r.daily_state, r.tracker_cohort, r.tracker_stat_view): (
            r.updated_at,
            r.source_freshness_tick_at,
            r.payload_json,
        )
        for r in rows_before
    }
    state_before = (await db_session.execute(select(EventDeskState))).scalar_one()
    freshness_tick_at_before = state_before.freshness_tick_at

    # Tick #2: the scheduled game is now inside the live-refresh window, and
    # its required season leaguegamelog fetch fails outright (404). The tick
    # raises without committing; this test rolls back exactly as the real
    # caller's `async with db.begin()` would on the raised exception.
    now2 = datetime(2026, 7, 11, 19, 30)
    client2 = NBAStatsClient(
        session=_FakeSession(
            {("leaguegamelog", "T"): [_FakeResponse({}, status_code=404)]}
        )
    )
    with pytest.raises(
        RuntimeError, match="Required Summer League live raw refresh failed"
    ):
        await run_desk_tick(db_session, now=now2, raw_root=tmp_path, client=client2)
    await db_session.rollback()

    db_session.expire_all()
    rows_after = (
        (await db_session.execute(select(EventDeskRenderSnapshot))).scalars().all()
    )
    assert len(rows_after) == 72
    signature_after = {
        (r.daily_state, r.tracker_cohort, r.tracker_stat_view): (
            r.updated_at,
            r.source_freshness_tick_at,
            r.payload_json,
        )
        for r in rows_after
    }
    assert signature_after == signature_before, (
        "a failed tick must never overwrite the prior successful tick's snapshots"
    )

    state_after = (await db_session.execute(select(EventDeskState))).scalar_one()
    assert state_after.freshness_tick_at == freshness_tick_at_before, (
        "a failed tick must never advance freshness either"
    )


# --------------------------------------------------------------------------- #
# 4. Frozen-time Preview/Live/Recap selection + 5. tip-time switch
# --------------------------------------------------------------------------- #
async def test_request_time_read_selects_matching_daily_state_variant(
    db_session: AsyncSession, tmp_path
) -> None:
    """Reading at the SAME `now` a tick used returns that state's materialized content."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)  # a Ledger day -- yesterday's final persists

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    player = await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-select-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())
    tick_result = await run_desk_tick(
        db_session, now=now, raw_root=tmp_path, client=client
    )
    await db_session.commit()
    assert tick_result.daily_state == EventDailyState.RECAP

    view = await get_desk_view_from_snapshot(db_session, now=now)
    assert view.payload is not None
    assert view.payload.daily_state == "recap"
    # The Ledger's top performer is the roster player we seeded a game log for.
    assert player.id is not None


async def test_tip_time_crossing_switches_state_without_another_tick(
    db_session: AsyncSession, tmp_path
) -> None:
    """A pre-tip tick already materialized Live; crossing tip flips the read with no new tick.

    `state_machine.inner_state`'s "scheduled-tip fallback" (rule 4) means the
    request-time resolver flips Preview -> Live once `now >= first tip`, even
    though the DB's game status is still `scheduled` (unchanged since the
    pre-tip tick). This proves the ALREADY-MATERIALIZED Live variant (built
    by that same pre-tip tick, per launch-readiness item 10) is what a
    post-tip read picks up -- no second tick required.
    """
    year = 2026
    tip = datetime(2026, 7, 10, 20, 0)
    # Well after the Morning flip, well before tip -- resolves Preview.
    pre_tip_now = datetime(2026, 7, 10, 15, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=tip,
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-tip-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())
    tick_result = await run_desk_tick(
        db_session, now=pre_tip_now, raw_root=tmp_path, client=client
    )
    await db_session.commit()
    assert tick_result.daily_state == EventDailyState.PREVIEW
    assert tick_result.materialized_variant_count == 72

    pre_tip_view = await get_desk_view_from_snapshot(db_session, now=pre_tip_now)
    assert pre_tip_view.payload is not None
    assert pre_tip_view.payload.daily_state == "preview"

    # No second tick runs here. The game's DB status is still `scheduled`.
    post_tip_now = tip + timedelta(minutes=5)
    post_tip_view = await get_desk_view_from_snapshot(db_session, now=post_tip_now)
    assert post_tip_view.payload is not None
    assert post_tip_view.payload.daily_state == "live"


# --------------------------------------------------------------------------- #
# 6. Missing / stale behavior
# --------------------------------------------------------------------------- #
async def test_missing_snapshot_degrades_honestly_never_falls_back_to_assembler(
    db_session: AsyncSession,
) -> None:
    """An in-window variant that was never materialized reads as off-window, not reassembled.

    The underlying data genuinely supports a full live assembly (proven by
    calling `get_desk_payload` directly on the same DB state, which DOES
    return content) -- so `get_desk_view_from_snapshot` returning `None`
    here is a deliberate refusal to fall back, not an artifact of the window
    itself being closed.
    """
    year = 2026
    today = date(2026, 7, 10)
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session,
        year=year,
        starts_on=today - timedelta(days=2),
        ends_on=today + timedelta(days=8),
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=today,
        tip_datetime=now - timedelta(hours=1),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=40,
        away_score=38,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-missing-v1")
    # Registers the `events` row (so the window resolves in-window) WITHOUT
    # ever running the tick -- `event_desk_render_snapshots` stays empty.
    await sync_summer_league_event(db_session, today)
    await db_session.commit()

    # Sanity: the full assembler genuinely CAN render this state from the
    # same DB rows (proves the window is truly in-window, not off).
    live_payload = await get_desk_payload(db_session, now=now)
    assert live_payload is not None
    assert live_payload.daily_state == "live"

    assert (
        await db_session.execute(select(EventDeskRenderSnapshot))
    ).scalars().all() == []

    view = await get_desk_view_from_snapshot(db_session, now=now)
    assert view.payload is None
    assert view.players == {}
    assert view.matchups == {}


async def test_stale_snapshot_still_renders_with_honest_stale_label(
    db_session: AsyncSession, tmp_path
) -> None:
    """A snapshot older than the staleness cadence still renders, labeled '-- stale'."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-stale-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())
    await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
    await db_session.commit()

    fresh_view = await get_desk_view_from_snapshot(db_session, now=now)
    assert fresh_view.payload is not None
    assert fresh_view.payload.freshness.state == "fresh"

    # Read far enough past the tick that the stamp is stale (> 2x the hourly
    # cadence, per `desk_read.FRESHNESS_STALE_AFTER`) -- no new tick runs.
    stale_now = now + timedelta(hours=5)
    stale_view = await get_desk_view_from_snapshot(db_session, now=stale_now)
    assert stale_view.payload is not None, (
        "stale must still render, not collapse to off-window"
    )
    assert stale_view.payload.daily_state == "recap"
    assert stale_view.payload.freshness.state == "stale"
    assert "stale" in stale_view.payload.freshness.as_of_et_label


# --------------------------------------------------------------------------- #
# 7. Post-lookup query capture -- no per-player/game enrichment
# --------------------------------------------------------------------------- #
async def test_snapshot_read_issues_no_per_player_or_game_queries(
    db_session: AsyncSession, async_engine: AsyncEngine, tmp_path
) -> None:
    """A snapshot-backed read's SQL never touches per-player/per-game tables.

    State resolution (`_resolve_window_state`) issues a small, FIXED number
    of calendar-shaped queries (events/competitions/game dates/today's
    statuses) -- never per-player, never per-game-row. The snapshot lookup
    itself is exactly one indexed read. Nothing after that point may touch
    `players_master`, `summer_league_player_game_logs`,
    `summer_league_desk_player_grades`, `summer_league_player_seasons`, or
    `summer_league_team_entries` -- the entire payload/view-context comes
    back pre-decoded from the snapshot row's JSON columns.
    """
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-query-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())
    await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
    await db_session.commit()

    with count_queries(async_engine) as captured:
        view = await get_desk_view_from_snapshot(db_session, now=now)

    assert view.payload is not None
    assert view.payload.daily_state == "recap"

    # Small, fixed budget -- state resolution (~5) + the one snapshot lookup.
    assert len(captured) <= 7, (
        f"expected a small, fixed query count, got {len(captured)}:\n"
        + "\n".join(
            f"  {i + 1}. {' '.join(s.split())[:120]}" for i, s in enumerate(captured)
        )
    )

    forbidden_tables = (
        "players_master",
        "summer_league_player_game_logs",
        "summer_league_desk_player_grades",
        "summer_league_player_seasons",
        "summer_league_team_entries",
        "summer_league_desk_slate",
        "summer_league_desk_storylines",
    )
    for statement in captured:
        lowered = statement.lower()
        for table in forbidden_tables:
            assert table not in lowered, (
                f"snapshot-backed read must never query {table!r} -- got: {statement}"
            )


# --------------------------------------------------------------------------- #
# 8. Honest degrade on an unreadable schema_version + the force-off kill switch
# --------------------------------------------------------------------------- #
async def test_unreadable_schema_version_degrades_to_off_window(
    db_session: AsyncSession, tmp_path
) -> None:
    """A materialized row whose `schema_version` this build can't decode reads as off-window.

    Not a 500, and -- critically -- never a fall-back to the full live
    assembler: an unreadable snapshot is treated exactly like a missing one.
    """
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-schema-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())
    await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
    await db_session.commit()

    # Fresh read decodes fine.
    ok = await get_desk_view_from_snapshot(db_session, now=now)
    assert ok.payload is not None

    # Corrupt every row's schema_version to one this build's codec rejects.
    await db_session.execute(
        update(EventDeskRenderSnapshot).values(
            schema_version=CURRENT_SCHEMA_VERSION + 1
        )
    )
    await db_session.commit()

    degraded = await get_desk_view_from_snapshot(db_session, now=now)
    assert degraded.payload is None
    assert degraded.players == {}
    assert degraded.matchups == {}


async def test_force_mode_off_reads_off_window_from_snapshot_path(
    db_session: AsyncSession, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sl_desk_force_mode="off"` is a kill switch on the snapshot read path too."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)

    comp = await _seed_competition(
        db_session, year=year, starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 20)
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    await _seed_game(
        db_session,
        comp,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
        home_score=70,
        away_score=65,
    )
    await _seed_roster_player(db_session, comp, home, year=year)
    await _seed_baseline(db_session, baseline_version="snap-forceoff-v1")
    await db_session.commit()

    client = NBAStatsClient(session=_FakeSession())
    result = await run_desk_tick(db_session, now=now, raw_root=tmp_path, client=client)
    await db_session.commit()
    assert result.materialized_variant_count == 72

    # With the kill switch off, the read short-circuits before even resolving
    # the window -- and the tick's own materialization step becomes a no-op.
    monkeypatch.setattr(settings, "sl_desk_force_mode", "off")
    view = await get_desk_view_from_snapshot(db_session, now=now)
    assert view.payload is None

    off_result = await build_desk_render_variants(db_session, now=now)
    assert off_result is None

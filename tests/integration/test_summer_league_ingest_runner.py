"""Integration tests for the Summer League ingest cron's schedule-refresh step.

`app/cli/summer_league_ingest_runner.py` (the hourly Summer League ingestion
cron) additively refreshes the active event's *forward* schedule
(``scheduleleaguev2``, via
``app.services.summer_league.scoreboard_ingest.run_scoreboard_ingest``) so
``summer_league_games.tip_datetime`` stays fresh independent of the Summer
League Desk tick's own lifecycle-gated step 0. These tests prove the two
halves of that behavior against a real Postgres session:

* In/near its window, the step runs and upserts real games with
  ``tip_datetime`` -- using the same REAL captured 2026 schedule fixture
  (``scheduleleaguev2_15_2026_live_pretip.json``) as
  ``tests/integration/test_summer_league_scoreboard_ingest.py`` and
  ``tests/integration/test_sl_desk_tick.py``, and a fake/recording NBA Stats
  client (no live network call).
* Off-window, the step makes NO network call at all -- mirrors
  `test_sl_desk_tick.py`'s
  `test_desk_tick_far_off_competition_stays_dormant_without_bootstrap`
  ("no fake client injected, so a regression would attempt a real call and
  fail" pattern). `_refresh_schedule`'s signature always requires an
  already-opened client (this runner's whole point is reusing the one
  ``main()`` opens for its venue loop, never opening a second one), so
  instead of omitting the client entirely a tripwire stand-in is injected:
  any call into it fails the test outright.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli import summer_league_ingest_runner as runner
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.utils.network_guard import transaction_depth
from app.schemas.summer_league_pipeline import SummerLeagueBatchPhase
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.batch_progress import (
    get_completed_batch_game_ids,
    invalidate_batch_progress,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.normalization import (
    normalize_competition_games,
    normalize_shot_events,
)
from app.services.summer_league.player_resolution import (
    apply_source_player_resolution_plan as _real_apply_source_player_resolution_plan,
)
from app.services.summer_league.raw_ingestion import dirty_game_ids_from_manifest
from app.services.summer_league.write_lock import (
    try_acquire_summer_league_writer_lock,
)

pytestmark = pytest.mark.asyncio

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
_REAL_LIVE_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


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

    Routes responses by the request's ``LeagueID`` param, mirroring
    ``tests/integration/test_summer_league_scoreboard_ingest.py``'s fake.
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


class _NetworkTripwireClient:
    """Client stand-in that fails the test if any of its methods are invoked.

    Stronger, more deterministic proof than "would try to open a real client
    and hang" (the pattern `test_sl_desk_tick.py` relies on for functions
    that own an *optional* client): `_refresh_schedule` always receives an
    already-opened client, so this fake is passed in its place and asserts
    directly that nothing ever calls into it.
    """

    def fetch_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(
            "no schedule/scoreboard network call should be attempted off-window"
        )

    def close(self) -> None:
        raise AssertionError("off-window run should never even close a client here")


async def _seed_competition(
    db: AsyncSession,
    *,
    year: int,
    league_id: str = "15",
    venue_slug: str = "las_vegas",
    starts_on: date | None,
    ends_on: date | None,
) -> SummerLeagueCompetition:
    idx = _next_idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"{venue_slug}-{idx}",
        display_name=f"{year} {venue_slug}",
        starts_on=starts_on,
        ends_on=ends_on,
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def test_refresh_schedule_in_window_upserts_games_with_tip_datetime(
    db_session: AsyncSession,
) -> None:
    """An in-window run calls ``run_scoreboard_ingest`` and persists real tip times.

    Seeds a 2026 Las Vegas competition whose ``starts_on``/``ends_on`` (as
    `normalization.refresh_competition_date_window` would have already set
    them from some earlier real game) span the real fixture's full schedule,
    with zero ``summer_league_games`` rows yet -- the exact "cron runs before
    the Desk has ever woken up" scenario this ticket decouples. `now` falls
    squarely inside that window (Active phase), so
    `runner._schedule_pull_in_window` should allow the call.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        league_id="15",
        starts_on=date(year, 7, 9),
        ends_on=date(year, 7, 19),
    )
    await db_session.commit()

    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []
    # `_refresh_schedule` opens its own `db.begin()` (mirrors `_run_venue`'s
    # per-step transaction, isolated from the venue loop) -- commit here so
    # the read above doesn't leave an ambient auto-begun transaction that
    # would conflict with it.
    await db_session.commit()

    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    now = datetime(year, 7, 12, 18, 0, tzinfo=timezone.utc)  # mid-event, 2:00pm ET
    report = await runner._refresh_schedule(db_session, now=now, client=client)
    await db_session.commit()

    assert report is not None
    assert report.games_created == 76
    assert report.errors == []

    # No live network call: the fake session recorded exactly one GET.
    assert len(session.calls) == 1
    _, called_params = session.calls[0]
    assert called_params["LeagueID"] == "15"

    games = (await db_session.execute(select(SummerLeagueGame))).scalars().all()
    assert len(games) == 76
    assert all(g.tip_datetime is not None for g in games)

    first_game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522600001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    # tip_datetime is stored naive (UTC by convention, like the rest of this
    # schema); the fixture's first game's real gameDateTimeUTC.
    assert first_game.tip_datetime == datetime(year, 7, 9, 19, 30)
    assert first_game.game_date == date(year, 7, 9)


async def test_schedule_pull_in_window_true_for_seeded_active_competition(
    db_session: AsyncSession,
) -> None:
    """Direct DB-driven check: the window guard reads real ``starts_on``/``ends_on``.

    No monkeypatching of `resolve_target_competitions` here (unlike the unit
    tests) -- proves the guard's own query resolves a real competition row
    and feeds it through `lifecycle_phase` correctly.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=date(year, 7, 9),
        ends_on=date(year, 7, 19),
    )
    await db_session.commit()

    now = datetime(year, 7, 12, 18, 0, tzinfo=timezone.utc)
    assert await runner._schedule_pull_in_window(db_session, now=now) is True


async def test_refresh_schedule_off_window_makes_no_network_call(
    db_session: AsyncSession,
) -> None:
    """A competition far outside its window -- no schedule/scoreboard network call.

    No games exist before or after the call, and the tripwire client proves
    `run_scoreboard_ingest` (and therefore any NBA Stats request) was never
    reached.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=date(year, 7, 9),
        ends_on=date(year, 7, 19),
    )
    await db_session.commit()

    # Three months before `announce_horizon_days` (14) even opens the window
    # -- mirrors `test_sl_desk_tick.py`'s far-off-dormant test.
    now = datetime(year, 3, 1, 12, 0, tzinfo=timezone.utc)

    result = await runner._refresh_schedule(
        db_session,
        now=now,
        client=_NetworkTripwireClient(),  # type: ignore[arg-type]
    )
    await db_session.commit()

    assert result is None
    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []


# ---------------------------------------------------------------------------
# `_run_venue` writer-lock containment (ticket #625): the writer lock must be
# released and reacquired between backbone / shot-batch / PBP-batch phases,
# not held continuously across all three as it was before this ticket (the
# 87.7-minute production incident -- see
# docs/plans/summer-league-cron-desk-starvation-spec.md). `try_acquire_summer_league_writer_lock`
# and `desk_is_waiting` are real (genuine Postgres advisory-lock calls); only
# the backbone/shot/PBP *business logic* is stubbed, since that correctness
# is already covered by `tests/integration/services/test_shotchart_ingest.py`,
# `test_pbp_ingest.py`, and `test_summer_league_normalization.py` -- this
# test isolates the transaction/lock-boundary behavior specifically.
# ---------------------------------------------------------------------------


class _FakeManifest:
    """Minimal stand-in for SummerLeagueRawManifest fetch results."""

    def __init__(self, game_ids: list[str]) -> None:
        self.game_ids = game_ids
        self.files_written: list[str] = []
        self.files_skipped: list[str] = []
        self.errors: list[str] = []

    @property
    def game_count(self) -> int:
        return len(self.game_ids)


class _FakeIngestor:
    """Fake ingestor returning queued manifests without touching the network."""

    def __init__(self, manifests: list[_FakeManifest]) -> None:
        self._manifests = manifests

    def fetch_year_league(self, _options: object) -> _FakeManifest:
        return self._manifests.pop(0)


class _FakeCompetitionGamesReport:
    """Minimal normalized-competition report carrying a nonexistent local ID.

    ``competition_id=999999`` deliberately matches no real
    ``summer_league_competitions`` row -- `_retry_incomplete_team_boxes`'s
    own read (`find_incomplete_team_box_game_ids`) then legitimately finds
    zero incomplete games and returns immediately, so this test doesn't need
    a full backbone fixture just to reach that step harmlessly.
    """

    competition_id: int = 999999


class _FakeBackfillReport:
    """Minimal backbone report used by the lock-containment test."""

    competition_games = _FakeCompetitionGamesReport()


class _FakeShotReport:
    """Minimal shot-event report used by the lock-containment test."""

    shot_events_upserted = 0
    games_with_shots = 0
    games_processed = 0


class _FakePBPReport:
    """Minimal PBP-event report used by the lock-containment test."""

    pbp_events_upserted = 0
    games_with_pbp = 0
    games_processed = 0


async def _probe_writer_lock_is_free(
    session_factory: async_sessionmaker[AsyncSession], test_schema: str
) -> bool:
    """From a fresh session/connection, report whether the writer lock is free.

    Uses its own session (not ``db_session``) so this is a genuine
    cross-connection Postgres advisory-lock probe, matching
    ``tests/integration/test_summer_league_write_lock.py``'s pattern.
    """
    async with session_factory() as probe:
        await probe.execute(text(f'SET search_path TO "{test_schema}"'))
        await probe.commit()
        async with probe.begin():
            return await try_acquire_summer_league_writer_lock(probe)


@pytest.mark.committed_db
async def test_run_venue_releases_writer_lock_between_phases(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second session can acquire the writer lock between phases, not just after.

    Under the pre-#625 shape (one ``db.begin()`` spanning backbone + shot +
    PBP), a probing second session could never observe the lock free until
    the *entire* venue finished. Here it must observe the lock held during
    each phase's own write, but free at the two boundaries in between.
    """
    lock_states: dict[str, bool] = {}
    completed_call_count = {"n": 0}

    async def _fake_backbone(_db: object, _options: object) -> object:
        lock_states["during_backbone"] = await _probe_writer_lock_is_free(
            session_factory, test_schema
        )
        return _FakeBackfillReport()

    async def _fake_get_completed_batch_game_ids(
        _db: object, **_kwargs: object
    ) -> set[str]:
        completed_call_count["n"] += 1
        if completed_call_count["n"] == 1:
            lock_states["between_backbone_and_shot"] = await _probe_writer_lock_is_free(
                session_factory, test_schema
            )
        elif completed_call_count["n"] == 2:
            lock_states["between_shot_and_pbp"] = await _probe_writer_lock_is_free(
                session_factory, test_schema
            )
        return set()

    async def _fake_shot(_db: object, **_kwargs: object) -> object:
        lock_states["during_shot"] = await _probe_writer_lock_is_free(
            session_factory, test_schema
        )
        return _FakeShotReport()

    async def _fake_pbp(_db: object, **_kwargs: object) -> object:
        lock_states["during_pbp"] = await _probe_writer_lock_is_free(
            session_factory, test_schema
        )
        return _FakePBPReport()

    async def _fake_record_batch_progress(_db: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "backfill_summer_league_backbone", _fake_backbone)
    monkeypatch.setattr(runner, "summarize_backfill_report", lambda _r: "summary")
    monkeypatch.setattr(runner, "normalize_shot_events", _fake_shot)
    monkeypatch.setattr(runner, "normalize_pbp_events", _fake_pbp)
    monkeypatch.setattr(
        runner, "get_completed_batch_game_ids", _fake_get_completed_batch_game_ids
    )
    monkeypatch.setattr(runner, "record_batch_progress", _fake_record_batch_progress)

    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["1522400001"]),  # refresh
            _FakeManifest(game_ids=["1522400001"]),  # fetch
        ]
    )

    had_games, failed = await runner._run_venue(
        db_session,
        ingestor,  # type: ignore[arg-type]
        year=2024,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert lock_states == {
        "during_backbone": False,
        "between_backbone_and_shot": True,
        "during_shot": False,
        "between_shot_and_pbp": True,
        "during_pbp": False,
    }


# ---------------------------------------------------------------------------
# Identity-resolution lock-lifetime (ticket #632): the writer lock must not
# be held while candidate search (this venue's Gemini-equivalent provider
# call) runs, only while a resolution write batch actually persists its
# results -- the July 19, 2026 incident's root cause was exactly the
# opposite of this. `try_acquire_summer_league_writer_lock` is real; only the
# backbone/shot/PBP business logic and the candidate-search provider call are
# stubbed.
# ---------------------------------------------------------------------------


async def _seed_pending_source_player(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    person_id: str,
    raw_name: str,
) -> SummerLeagueSourcePlayer:
    """Seed one game-logged, still-unresolved source player for this venue/year."""
    competition = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"lock-lifetime-{_next_idx()}",
        display_name=f"{year} Lock Lifetime Venue",
        data_quality=SummerLeagueDataQuality.FULL,
    )
    db.add(competition)
    await db.flush()
    assert competition.id is not None

    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"999{_next_idx():04d}",
        raw_team_name="Lock Lifetime Team",
        raw_team_abbreviation="LLT",
        team_slug=f"lock-lifetime-team-{_next_idx()}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None

    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"lock-lifetime-{_next_idx()}",
        game_date=date(year, 7, 12),
        home_team_entry_id=team.id,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None

    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=person_id,
        raw_player_name=raw_name,
        normalized_name=_normalized_name_key(raw_name),
        first_seen_year=year,
        last_seen_year=year,
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None

    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=None,
            nba_stats_person_id=person_id,
            raw_player_name=raw_name,
            minutes_seconds=1200,
            pts=10,
            source_endpoint="boxscoretraditionalv2",
        )
    )
    await db.flush()
    return source_player


@pytest.mark.committed_db
async def test_run_venue_resolution_search_runs_without_lock_writes_hold_it(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate search runs lock-free; applying its result briefly holds the lock.

    Regression test for ticket #632: the July 19, 2026 incident's root cause
    was candidate search (this venue's Gemini-bound provider call) running
    inside the same transaction/writer lock as the rest of backbone
    normalization. A second session must be able to acquire the writer lock
    *while candidate search is in flight*, and the source player's stub
    creation must actually persist once the (lock-held) write batch runs.
    """
    source_player = await _seed_pending_source_player(
        db_session,
        year=2024,
        league_id="15",
        person_id="9641099",
        raw_name="Lock Lifetime Prospect",
    )
    await db_session.commit()  # close the seeding autobegun transaction

    lock_states: dict[str, bool] = {}

    async def _fake_backbone(_db: object, _options: object) -> object:
        return _FakeBackfillReport()

    async def _fake_shot(_db: object, **_kwargs: object) -> object:
        return _FakeShotReport()

    async def _fake_pbp(_db: object, **_kwargs: object) -> object:
        return _FakePBPReport()

    async def _fake_candidate_search(
        _db: object, _query: str, k: int = 5
    ) -> list[object]:
        lock_states["candidate_transaction_free"] = transaction_depth() == 0
        lock_states["during_candidate_search"] = await _probe_writer_lock_is_free(
            session_factory, test_schema
        )
        return []

    async def _spy_apply_resolution_plan(
        db_arg: AsyncSession, sp: object, plan: object, **kwargs: object
    ) -> object:
        lock_states["during_resolution_write"] = await _probe_writer_lock_is_free(
            session_factory, test_schema
        )
        return await _real_apply_source_player_resolution_plan(
            db_arg, sp, plan, **kwargs  # type: ignore[arg-type]
        )

    monkeypatch.setattr(runner, "backfill_summer_league_backbone", _fake_backbone)
    monkeypatch.setattr(runner, "summarize_backfill_report", lambda _r: "summary")
    monkeypatch.setattr(runner, "normalize_shot_events", _fake_shot)
    monkeypatch.setattr(runner, "normalize_pbp_events", _fake_pbp)
    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        _fake_candidate_search,
    )
    monkeypatch.setattr(
        runner, "apply_source_player_resolution_plan", _spy_apply_resolution_plan
    )

    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["1522400001"]),  # refresh
            _FakeManifest(game_ids=["1522400001"]),  # fetch
        ]
    )

    had_games, failed = await runner._run_venue(
        db_session,
        ingestor,  # type: ignore[arg-type]
        year=2024,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert lock_states == {
        "candidate_transaction_free": True,
        "during_candidate_search": True,  # lock free -- no transaction held it
        "during_resolution_write": False,  # lock held -- the write batch owns it
    }

    await db_session.refresh(source_player)
    assert source_player.resolution_status == SummerLeagueResolutionStatus.STUB
    assert source_player.canonical_player_id is not None


# ---------------------------------------------------------------------------
# Batch resumability (ticket #625): a crash/interruption partway through a
# venue's shot normalization, followed by a re-run, must resume only the
# incomplete batches -- no replay of already-committed games, no duplicate
# rows, and the final state matches a clean uninterrupted run.
# ---------------------------------------------------------------------------


def _result_set(name: str, headers: list[str], rows: list[list[object]]) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


_RESUME_SHOT_HEADERS = [
    "GRID_TYPE",
    "GAME_ID",
    "GAME_EVENT_ID",
    "PLAYER_ID",
    "PLAYER_NAME",
    "TEAM_ID",
    "TEAM_NAME",
    "PERIOD",
    "MINUTES_REMAINING",
    "SECONDS_REMAINING",
    "EVENT_TYPE",
    "ACTION_TYPE",
    "SHOT_TYPE",
    "SHOT_ZONE_BASIC",
    "SHOT_ZONE_AREA",
    "SHOT_ZONE_RANGE",
    "SHOT_DISTANCE",
    "LOC_X",
    "LOC_Y",
    "SHOT_ATTEMPTED_FLAG",
    "SHOT_MADE_FLAG",
    "GAME_DATE",
    "HTM",
    "VTM",
]

_RESUME_GAME_IDS = ["1522400001", "1522400002", "1522400003"]


def _write_resumability_fixture(raw_root: Path) -> None:
    """Write a 3-game raw fixture, each with exactly one shot event.

    Mirrors ``tests/integration/services/test_shotchart_ingest.py``'s
    fixture pattern, extended to three games so a batch size of 1 (forced
    via monkeypatching ``runner.EVENT_BATCH_SIZE``) yields three
    independently committed batches to interrupt/resume across.
    """
    run_dir = raw_root / "2024" / "15"
    team_rows = []
    for index, game_id in enumerate(_RESUME_GAME_IDS, start=1):
        game_dir = run_dir / "games" / game_id
        game_dir.mkdir(parents=True)
        team_rows.append(
            [1610612753, "ORL", "Orlando Magic", game_id, "2024-07-12", "ORL vs. CLE", 100 + index]
        )
        team_rows.append(
            [1610612739, "CLE", "Cleveland Cavaliers", game_id, "2024-07-12", "CLE @ ORL", 90 + index]
        )
        game_dir.joinpath("boxscoretraditionalv2.json").write_text(
            json.dumps(
                {
                    "resultSets": [
                        _result_set(
                            "TeamStats",
                            [
                                "GAME_ID",
                                "TEAM_ID",
                                "TEAM_NAME",
                                "TEAM_ABBREVIATION",
                                "MIN",
                                "PTS",
                            ],
                            [
                                [game_id, 1610612753, "Magic", "ORL", "200:00", 100 + index],
                                [game_id, 1610612739, "Cavaliers", "CLE", "200:00", 90 + index],
                            ],
                        ),
                        _result_set("PlayerStats", [], []),
                    ]
                }
            )
        )
        game_dir.joinpath("boxscoreadvancedv2.json").write_text(
            json.dumps(
                {
                    "resultSets": [
                        _result_set("PlayerStats", [], []),
                        _result_set("TeamStats", [], []),
                    ]
                }
            )
        )
        game_dir.joinpath("boxscorescoringv2.json").write_text(
            json.dumps({"resultSets": [_result_set("sqlPlayersScoring", [], [])]})
        )
        game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))
        game_dir.joinpath("shotchartdetail.json").write_text(
            json.dumps(
                {
                    "resultSets": [
                        _result_set(
                            "Shot_Chart_Detail",
                            _RESUME_SHOT_HEADERS,
                            [
                                [
                                    "Shot Chart Detail",
                                    game_id,
                                    index,
                                    1640000 + index,
                                    f"Player {index}",
                                    1610612753,
                                    "Orlando Magic",
                                    1,
                                    9,
                                    30,
                                    "Made Shot",
                                    "Jump Shot",
                                    "2PT Field Goal",
                                    "Mid-Range",
                                    "Center(C)",
                                    "16-24 ft.",
                                    18,
                                    0,
                                    180,
                                    1,
                                    1,
                                    "2024-07-12",
                                    "ORL",
                                    "CLE",
                                ]
                            ],
                        )
                    ]
                }
            )
        )

    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": 2024,
                "league_id": "15",
                "venue": "las_vegas",
                "team_gamelog_rows": len(team_rows),
                "player_gamelog_rows": 0,
                "game_ids": _RESUME_GAME_IDS,
                "game_count": len(_RESUME_GAME_IDS),
                "errors": [],
            }
        )
    )
    run_dir.joinpath("leaguegamelog_team.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        [
                            "TEAM_ID",
                            "TEAM_ABBREVIATION",
                            "TEAM_NAME",
                            "GAME_ID",
                            "GAME_DATE",
                            "MATCHUP",
                            "PTS",
                        ],
                        team_rows,
                    )
                ]
            }
        )
    )
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps({"resultSets": []})
    )


async def test_run_batched_phase_resumes_only_incomplete_games_after_a_crash(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulated crash mid-phase, followed by a resume, replays nothing already committed.

    Three games are batched one-per-batch (``EVENT_BATCH_SIZE`` forced to 1).
    The second batch's ``normalize_shot_events`` call is made to raise,
    simulating a crash after the first batch already committed. A resumed
    call with the real (non-raising) normalizer must then process only the
    two remaining games -- proven both by which ``game_ids`` batches the
    resumed run is passed, and by the final row count/content matching a
    clean, uninterrupted 3-game run (one shot event per game, no
    duplicates).
    """
    from app.schemas.summer_league import SummerLeagueShotEvent

    monkeypatch.setattr(runner, "EVENT_BATCH_SIZE", 1)
    # `_run_batched_phase` always sources raw files from the module-level
    # `RAW_ROOT` constant (mirroring `_run_venue`'s own behavior), not a
    # parameter -- point it at this test's fixture directory.
    monkeypatch.setattr(runner, "RAW_ROOT", tmp_path)
    _write_resumability_fixture(tmp_path)
    await audit_summer_league_raw(db_session, raw_root=tmp_path, year=2024, league_id="15")
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    call_count = {"n": 0}

    async def _crash_on_second_batch(db: AsyncSession, **kwargs: object) -> object:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash mid-batch")
        return await normalize_shot_events(db, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="simulated crash"):
        await runner._run_batched_phase(
            db_session,
            year=2024,
            league_id="15",
            phase=SummerLeagueBatchPhase.SHOT,
            game_ids=_RESUME_GAME_IDS,
            normalize=_crash_on_second_batch,
            describe=lambda _report: "",
            telemetry=None,
        )

    # Exactly the first game's batch committed durably before the crash.
    completed_after_crash = await get_completed_batch_game_ids(
        db_session, year=2024, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed_after_crash == {_RESUME_GAME_IDS[0]}
    shots_after_crash = (
        (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    )
    assert {s.nba_stats_game_id for s in shots_after_crash} == {_RESUME_GAME_IDS[0]}

    # Resume: same full game universe, real (non-raising) normalizer this time.
    resumed_batches: list[set[str]] = []

    async def _recording_normalize(db: AsyncSession, **kwargs: object) -> object:
        resumed_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return await normalize_shot_events(db, **kwargs)  # type: ignore[arg-type]

    completed_fully = await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=_recording_normalize,
        describe=lambda _report: "",
        telemetry=None,
    )

    assert completed_fully is True
    # The already-completed first game is never revisited on resume.
    assert resumed_batches == [{_RESUME_GAME_IDS[1]}, {_RESUME_GAME_IDS[2]}]

    completed_after_resume = await get_completed_batch_game_ids(
        db_session, year=2024, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed_after_resume == set(_RESUME_GAME_IDS)

    final_shots = (
        (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    )
    # One shot event per game, matching a clean uninterrupted run -- no
    # duplicates, nothing skipped.
    assert len(final_shots) == len(_RESUME_GAME_IDS)
    assert {s.nba_stats_game_id for s in final_shots} == set(_RESUME_GAME_IDS)
    pairs = [(s.nba_stats_game_id, s.nba_stats_game_event_id) for s in final_shots]
    assert len(pairs) == len(set(pairs))


async def test_refresh_schedule_cold_start_no_configured_window_makes_no_network_call(
    db_session: AsyncSession,
) -> None:
    """A competition registered with no ``starts_on``/``ends_on`` yet -- no network call.

    The true first-ever cold start (mirrors
    `runner._schedule_pull_in_window`'s own docstring trade-off): with no
    real games and no configured window, there is no signal to reason about,
    so this stays network-free rather than guessing.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=None,
        ends_on=None,
    )
    await db_session.commit()

    now = datetime(year, 7, 12, 18, 0, tzinfo=timezone.utc)

    result = await runner._refresh_schedule(
        db_session,
        now=now,
        client=_NetworkTripwireClient(),  # type: ignore[arg-type]
    )
    await db_session.commit()

    assert result is None
    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []


# ---------------------------------------------------------------------------
# Dirty-game reprocessing (ticket #626): closes the gap ticket #625 opened --
# SummerLeagueBatchProgress rows durably skip an already-completed game on
# every later run, but that permanence meant a game whose raw file changed
# again (a forced re-fetch correcting a bad snapshot) was silently skipped
# forever. These tests prove the fix end-to-end against a real Postgres
# session, reusing the resumability fixture (three games, one shot event
# each) already established above.
# ---------------------------------------------------------------------------


async def test_dirty_game_reprocessed_after_raw_shotchart_correction(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed game's corrected shotchartdetail file is reprocessed, not skipped forever.

    A clean run completes SHOT batching for all three games. Game 1's
    shotchartdetail.json is then rewritten with a corrected shot outcome
    (made -> missed), mirroring what a forced re-fetch (e.g.
    `_retry_incomplete_team_boxes`-style correction) would produce.
    `dirty_game_ids_from_manifest` + `invalidate_batch_progress` clear only
    that game's SHOT progress row; a subsequent `_run_batched_phase` call
    must then reprocess exactly that one game -- updating the existing shot
    row in place (same id, `made` flipped) -- while leaving the other two
    untouched games' rows alone.
    """
    monkeypatch.setattr(runner, "RAW_ROOT", tmp_path)
    _write_resumability_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    # Clean run: all three games' SHOT batches commit.
    completed_fully = await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=normalize_shot_events,
        describe=lambda _report: "",
        telemetry=None,
    )
    assert completed_fully is True
    completed = await get_completed_batch_game_ids(
        db_session, year=2024, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed == set(_RESUME_GAME_IDS)

    corrected_game_id = _RESUME_GAME_IDS[0]
    before = (
        await db_session.execute(
            select(SummerLeagueShotEvent).where(
                SummerLeagueShotEvent.nba_stats_game_id  # type: ignore[arg-type]
                == corrected_game_id
            )
        )
    ).scalar_one()
    assert before.made is True

    # Simulate a corrected raw snapshot: the shot flips from made to missed.
    corrected_path = (
        tmp_path
        / "2024"
        / "15"
        / "games"
        / corrected_game_id
        / "shotchartdetail.json"
    )
    corrected_payload = json.loads(corrected_path.read_text())
    row = corrected_payload["resultSets"][0]["rowSet"][0]
    made_index = _RESUME_SHOT_HEADERS.index("SHOT_MADE_FLAG")
    row[made_index] = 0
    corrected_path.write_text(json.dumps(corrected_payload))

    # The manifest a real forced re-fetch would have produced: only the
    # corrected game's shotchartdetail file was actually written this time.
    fetch_manifest = _FakeManifest(game_ids=list(_RESUME_GAME_IDS))
    fetch_manifest.files_written = [
        f"2024/15/games/{corrected_game_id}/shotchartdetail.json"
    ]

    dirty_shot_ids = dirty_game_ids_from_manifest(
        fetch_manifest,  # type: ignore[arg-type]
        endpoints=("shotchartdetail",),
    )
    assert dirty_shot_ids == {corrected_game_id}
    await invalidate_batch_progress(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=dirty_shot_ids,
    )
    await db_session.commit()

    # Only the corrected game is now in the "remaining" set for SHOT.
    resumed_batches: list[set[str]] = []

    async def _recording_normalize(db: AsyncSession, **kwargs: object) -> object:
        resumed_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return await normalize_shot_events(db, **kwargs)  # type: ignore[arg-type]

    completed_fully = await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=_recording_normalize,
        describe=lambda _report: "",
        telemetry=None,
    )

    assert completed_fully is True
    assert resumed_batches == [{corrected_game_id}]

    after = (
        await db_session.execute(
            select(SummerLeagueShotEvent).where(
                SummerLeagueShotEvent.nba_stats_game_id  # type: ignore[arg-type]
                == corrected_game_id
            )
        )
    ).scalar_one()
    assert after.id == before.id  # same row updated in place, not duplicated
    assert after.made is False

    # The other two, untouched games' rows are unaffected -- one row per game.
    all_shots = (
        (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    )
    assert len(all_shots) == len(_RESUME_GAME_IDS)


async def test_unchanged_venue_second_run_processes_zero_games(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard on #625: an unchanged venue's second run touches ~0 games."""
    monkeypatch.setattr(runner, "RAW_ROOT", tmp_path)
    _write_resumability_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    first_batches: list[set[str]] = []

    async def _recording_first(db: AsyncSession, **kwargs: object) -> object:
        first_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return await normalize_shot_events(db, **kwargs)  # type: ignore[arg-type]

    await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=_recording_first,
        describe=lambda _report: "",
        telemetry=None,
    )
    assert first_batches == [set(_RESUME_GAME_IDS)]

    # Second run: same discovered universe, nothing rewritten -- no dirty
    # detection would have anything to invalidate here either.
    fetch_manifest = _FakeManifest(game_ids=list(_RESUME_GAME_IDS))
    assert dirty_game_ids_from_manifest(fetch_manifest) == set()  # type: ignore[arg-type]

    second_batches: list[set[str]] = []

    async def _recording_second(db: AsyncSession, **kwargs: object) -> object:
        second_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return await normalize_shot_events(db, **kwargs)  # type: ignore[arg-type]

    completed_fully = await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=_recording_second,
        describe=lambda _report: "",
        telemetry=None,
    )

    assert completed_fully is True
    assert second_batches == []  # zero games processed -- nothing new, nothing dirty


async def test_full_reconcile_reprocesses_every_completed_game(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SL_INGEST_FULL_RECONCILE clears all progress and forces a complete reprocess.

    Exercises `runner._reconcile_batch_progress` directly with
    `full_reconcile=True` and a fetch manifest with nothing dirty -- proving
    the full-reconciliation path is independent of, and overrides, ordinary
    dirty detection.
    """
    monkeypatch.setattr(runner, "RAW_ROOT", tmp_path)
    _write_resumability_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=normalize_shot_events,
        describe=lambda _report: "",
        telemetry=None,
    )
    completed = await get_completed_batch_game_ids(
        db_session, year=2024, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed == set(_RESUME_GAME_IDS)
    await db_session.commit()

    # No raw files changed -- an unmodified fetch manifest -- yet
    # full_reconcile=True must still clear every progress row.
    fetch_manifest = _FakeManifest(game_ids=list(_RESUME_GAME_IDS))

    await runner._reconcile_batch_progress(
        db_session,
        year=2024,
        league_id="15",
        fetch_manifest=fetch_manifest,  # type: ignore[arg-type]
        full_reconcile=True,
    )
    await db_session.commit()

    assert (
        await get_completed_batch_game_ids(
            db_session, year=2024, league_id="15", phase=SummerLeagueBatchPhase.SHOT
        )
        == set()
    )

    resumed_batches: list[set[str]] = []

    async def _recording_normalize(db: AsyncSession, **kwargs: object) -> object:
        resumed_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return await normalize_shot_events(db, **kwargs)  # type: ignore[arg-type]

    completed_fully = await runner._run_batched_phase(
        db_session,
        year=2024,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=_RESUME_GAME_IDS,
        normalize=_recording_normalize,
        describe=lambda _report: "",
        telemetry=None,
    )

    assert completed_fully is True
    assert resumed_batches == [set(_RESUME_GAME_IDS)]  # every game reprocessed

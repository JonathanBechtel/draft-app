"""Unit tests for the Summer League ingestion cron runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pytest

from app.cli import summer_league_ingest_runner as runner
from app.schemas.summer_league import SummerLeagueCompetition


@dataclass
class _FakeEnvironmentRefreshOutcome:
    """Minimal stand-in for `EnvironmentRefreshOutcome` used by fake refreshes."""

    attempted: bool = True
    succeeded: bool = True
    built_scopes: int = 1


@pytest.fixture(autouse=True)
def _summer_league_writer_lock_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep unit fakes focused on runner behavior with an available DB writer lock."""

    async def _available(_db: object) -> bool:
        return True

    async def _defer(_db: object, **_kwargs: object) -> None:
        return None

    async def _pending(_db: object) -> bool:
        return False

    async def _complete(_db: object, **_kwargs: object) -> None:
        return None

    async def _record_failure(_db: object, **_kwargs: object) -> None:
        return None

    async def _available_yielding(
        _db: object, _step_fields: object = None
    ) -> bool:
        return True

    async def _no_completed_batches(_db: object, **_kwargs: object) -> set[str]:
        return set()

    async def _no_pending_batches(_db: object, **_kwargs: object) -> int:
        return 0

    async def _record_batch_progress(_db: object, **_kwargs: object) -> None:
        return None

    async def _invalidate_batch_progress(_db: object, **_kwargs: object) -> None:
        return None

    async def _fake_refresh_environment(
        _db: object, *, year: int, telemetry: object | None = None
    ) -> object:
        return _FakeEnvironmentRefreshOutcome()

    monkeypatch.setattr(runner, "try_acquire_summer_league_writer_lock", _available)
    monkeypatch.setattr(
        runner, "try_acquire_summer_league_writer_lock_yielding", _available_yielding
    )
    monkeypatch.setattr(runner, "defer_full_reconciliation", _defer)
    monkeypatch.setattr(runner, "full_reconciliation_is_pending", _pending)
    monkeypatch.setattr(runner, "complete_pipeline", _complete)
    monkeypatch.setattr(runner, "record_pipeline_failure", _record_failure)
    monkeypatch.setattr(runner, "get_completed_batch_game_ids", _no_completed_batches)
    monkeypatch.setattr(runner, "count_pending_batch_games", _no_pending_batches)
    monkeypatch.setattr(runner, "record_batch_progress", _record_batch_progress)
    monkeypatch.setattr(runner, "invalidate_batch_progress", _invalidate_batch_progress)
    monkeypatch.setattr(
        runner, "refresh_environment_profiles_for_year", _fake_refresh_environment
    )


@dataclass
class _FakeManifest:
    """Minimal stand-in for SummerLeagueRawManifest fetch results."""

    game_ids: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def game_count(self) -> int:
        return len(self.game_ids)


class _FakeBegin:
    """Async context manager standing in for ``AsyncSession.begin()``."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSession:
    """Minimal async session exposing ``begin()`` as a context manager."""

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def commit(self) -> None:
        """No-op, matching ``AsyncSession.commit()``'s interface."""

    async def rollback(self) -> None:
        """No-op, matching ``AsyncSession.rollback()``'s interface."""


class _FakeIngestor:
    """Fake ingestor returning queued manifests and recording call options."""

    def __init__(self, manifests: list[_FakeManifest] | Exception) -> None:
        self._manifests = manifests
        self.calls: list[object] = []

    def fetch_year_league(self, options: object) -> _FakeManifest:
        self.calls.append(options)
        if isinstance(self._manifests, Exception):
            raise self._manifests
        return self._manifests.pop(0)


# ---------------------------------------------------------------------------
# _resolve_year
# ---------------------------------------------------------------------------


def test_resolve_year_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent env var yields the default Summer League year."""
    monkeypatch.delenv("SL_INGEST_YEAR", raising=False)
    assert runner._resolve_year() == runner.DEFAULT_YEAR


def test_resolve_year_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SL_INGEST_YEAR overrides the default year."""
    monkeypatch.setenv("SL_INGEST_YEAR", "2025")
    assert runner._resolve_year() == 2025


def test_resolve_year_blank_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank/whitespace SL_INGEST_YEAR falls back to the default."""
    monkeypatch.setenv("SL_INGEST_YEAR", "   ")
    assert runner._resolve_year() == runner.DEFAULT_YEAR


@pytest.mark.parametrize("value", ["26", "20260", "abc"])
def test_resolve_year_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Non-four-digit or non-numeric SL_INGEST_YEAR raises ValueError.

    This must fail up front so a misconfigured schedule exits non-zero
    rather than silently ingesting nothing for every venue.
    """
    monkeypatch.setenv("SL_INGEST_YEAR", value)
    with pytest.raises(ValueError):
        runner._resolve_year()


def test_resolve_year_valid_four_digit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid four-digit season year is accepted."""
    monkeypatch.setenv("SL_INGEST_YEAR", "2025")
    assert runner._resolve_year() == 2025


# ---------------------------------------------------------------------------
# _resolve_league_ids
# ---------------------------------------------------------------------------


def test_resolve_league_ids_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent env var yields the default venue ordering."""
    monkeypatch.delenv("SL_INGEST_LEAGUE_IDS", raising=False)
    assert runner._resolve_league_ids() == ["13", "16", "15"]


def test_resolve_league_ids_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SL_INGEST_LEAGUE_IDS overrides the venue list, preserving order."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15,14")
    assert runner._resolve_league_ids() == ["15", "14"]


def test_resolve_league_ids_dedup_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace is trimmed and duplicate LeagueIDs collapse to first-seen."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", " 15 , 13 ,15, 13 ")
    assert runner._resolve_league_ids() == ["15", "13"]


def test_resolve_league_ids_all_blank_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-blank SL_INGEST_LEAGUE_IDS raises ValueError."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", " , , ")
    with pytest.raises(ValueError):
        runner._resolve_league_ids()


# ---------------------------------------------------------------------------
# _full_reconcile_requested
# ---------------------------------------------------------------------------


def test_full_reconcile_requested_defaults_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent SL_INGEST_FULL_RECONCILE -> routine dirty-detection-only path."""
    monkeypatch.delenv("SL_INGEST_FULL_RECONCILE", raising=False)
    assert runner._full_reconcile_requested() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_full_reconcile_requested_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A truthy SL_INGEST_FULL_RECONCILE value requests full reconciliation."""
    monkeypatch.setenv("SL_INGEST_FULL_RECONCILE", value)
    assert runner._full_reconcile_requested() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "  "])
def test_full_reconcile_requested_falsy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A falsy/blank SL_INGEST_FULL_RECONCILE value does not request it."""
    monkeypatch.setenv("SL_INGEST_FULL_RECONCILE", value)
    assert runner._full_reconcile_requested() is False


# ---------------------------------------------------------------------------
# _run_venue
# ---------------------------------------------------------------------------


def _patch_backbone_services(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    raise_in_backbone: bool = False,
    incomplete_game_ids: tuple[str, ...] = (),
    shot_batches: list[set[str]] | None = None,
    pbp_batches: list[set[str]] | None = None,
    resolution_pairs: list[tuple[object, object]] | None = None,
) -> None:
    """Monkeypatch the three DB-touching backbone/normalize services.

    ``resolution_pairs`` defaults to empty -- Phase A2 (see
    ``_run_resolution_phase``) then short-circuits before ever attempting the
    writer lock, which is what keeps every existing exact-sequence/exact-
    lock-attempt-count assertion in this module valid without having to
    account for a new resolution step.
    """

    async def _fake_backbone(_db: object, _options: object) -> object:
        calls.append("backbone")
        if raise_in_backbone:
            raise RuntimeError("backbone boom")
        return _FakeBackfillReport()

    async def _fake_prepare_resolutions(
        _db: object, **_kwargs: object
    ) -> list[tuple[object, object]]:
        return list(resolution_pairs or [])

    async def _fake_apply_resolution_plan(
        _db: object, _source_player: object, _plan: object, **_kwargs: object
    ) -> object:
        raise AssertionError(
            "apply_source_player_resolution_plan should not run when "
            "resolution_pairs is empty"
        )

    async def _fake_shot(_db: object, **kwargs: object) -> object:
        calls.append("shot")
        if shot_batches is not None:
            shot_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return _FakeShotReport()

    async def _fake_pbp(_db: object, **kwargs: object) -> object:
        calls.append("pbp")
        if pbp_batches is not None:
            pbp_batches.append(set(kwargs.get("game_ids") or set()))  # type: ignore[call-overload]
        return _FakePBPReport()

    async def _fake_competition(_db: object, **_kwargs: object) -> object:
        calls.append("competition")
        return _FakeCompetitionReport()

    async def _fake_find_incomplete(_db: object, *, competition_id: int) -> list[str]:
        assert competition_id == 123
        calls.append("find_incomplete")
        return list(incomplete_game_ids)

    monkeypatch.setattr(runner, "backfill_summer_league_backbone", _fake_backbone)
    monkeypatch.setattr(runner, "summarize_backfill_report", lambda _r: "summary")
    monkeypatch.setattr(
        runner, "prepare_summer_league_player_resolutions", _fake_prepare_resolutions
    )
    monkeypatch.setattr(
        runner, "apply_source_player_resolution_plan", _fake_apply_resolution_plan
    )
    monkeypatch.setattr(runner, "normalize_shot_events", _fake_shot)
    monkeypatch.setattr(runner, "normalize_pbp_events", _fake_pbp)
    monkeypatch.setattr(runner, "normalize_competition_games", _fake_competition)
    monkeypatch.setattr(
        runner, "find_incomplete_team_box_game_ids", _fake_find_incomplete
    )


@dataclass
class _FakeCompetitionReport:
    """Minimal normalized-competition report carrying its local database ID."""

    competition_id: int = 123
    team_game_logs_upserted: int = 4


@dataclass
class _FakeBackfillReport:
    """Minimal backbone report used by the venue runner tests."""

    competition_games: _FakeCompetitionReport | None = field(
        default_factory=_FakeCompetitionReport
    )


@dataclass
class _FakeShotReport:
    shot_events_upserted: int = 0
    games_with_shots: int = 0
    games_processed: int = 0


@dataclass
class _FakePBPReport:
    pbp_events_upserted: int = 0
    games_with_pbp: int = 0
    games_processed: int = 0


@pytest.mark.asyncio
async def test_run_venue_no_games(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty refresh manifest short-circuits without touching the backbone."""
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls)
    ingestor = _FakeIngestor([_FakeManifest(game_ids=[])])

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (False, False)
    assert calls == []  # backbone never invoked
    assert len(ingestor.calls) == 1  # only the season-index refresh happened


@pytest.mark.asyncio
async def test_run_venue_fetch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw-fetch exception is folded into the no-games no-op path."""
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls)
    ingestor = _FakeIngestor(RuntimeError("stats.nba.com unreachable"))

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (False, False)
    assert calls == []


@pytest.mark.asyncio
async def test_run_venue_with_games_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Games present with succeeding services returns (True, False)."""
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls)
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),  # refresh
            _FakeManifest(game_ids=["001"]),  # fetch
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert calls == ["backbone", "shot", "pbp", "find_incomplete"]


@pytest.mark.asyncio
async def test_run_venue_retries_incomplete_team_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback team rows force-refresh exact game IDs before re-normalization."""
    calls: list[str] = []
    _patch_backbone_services(
        monkeypatch,
        calls,
        incomplete_game_ids=("001", "002"),
    )
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001", "002"]),  # refresh
            _FakeManifest(game_ids=["001", "002"]),  # initial fetch
            _FakeManifest(game_ids=["001", "002"]),  # forced retry
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert calls == [
        "backbone",
        "shot",
        "pbp",
        "find_incomplete",
        "competition",
    ]
    assert len(ingestor.calls) == 3
    retry_options = ingestor.calls[2]
    assert isinstance(retry_options, runner.RawIngestionOptions)
    assert retry_options.force is True
    assert retry_options.game_ids == ("001", "002")
    assert retry_options.skip_endpoints == ("playbyplayv2", "shotchartdetail")


@pytest.mark.asyncio
async def test_run_venue_retry_yields_when_desk_writer_starts_during_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry rechecks the writer lock after network I/O and skips on contention."""
    calls: list[str] = []
    _patch_backbone_services(
        monkeypatch,
        calls,
        incomplete_game_ids=("001",),
    )
    attempts = {"n": 0}

    async def _lock_available_until_retry(
        _db: object, _step_fields: object = None
    ) -> bool:
        # A single-game venue makes exactly three writer-lock attempts
        # before the team-box retry's own reacquisition: one for the
        # backbone phase, one for the (single-batch) shot phase, and one
        # for the (single-batch) PBP phase. Only the fourth attempt -- the
        # retry's own -- should find the Desk writer active.
        attempts["n"] += 1
        return attempts["n"] <= 3

    monkeypatch.setattr(
        runner,
        "try_acquire_summer_league_writer_lock_yielding",
        _lock_available_until_retry,
    )
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),
            _FakeManifest(game_ids=["001"]),
            _FakeManifest(game_ids=["001"]),
        ]
    )

    result = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert result == (True, False)
    assert calls == ["backbone", "shot", "pbp", "find_incomplete"]
    assert len(ingestor.calls) == 3


@pytest.mark.asyncio
async def test_run_venue_skips_db_processing_while_desk_writer_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy Desk writer defers full reconciliation before shared DB writes."""
    calls: list[str] = []
    deferred_reasons: list[str] = []
    _patch_backbone_services(monkeypatch, calls)

    async def _busy(_db: object, _step_fields: object = None) -> bool:
        return False

    async def _defer(_db: object, *, reason: str) -> None:
        deferred_reasons.append(reason)

    monkeypatch.setattr(
        runner, "try_acquire_summer_league_writer_lock_yielding", _busy
    )
    monkeypatch.setattr(runner, "defer_full_reconciliation", _defer)
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),
            _FakeManifest(game_ids=["001"]),
        ]
    )

    result = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert result == (False, False)
    assert calls == []
    assert deferred_reasons == ["venue:15:shared_write_phase_lock_contended"]


@pytest.mark.asyncio
async def test_run_venue_resolution_batch_defers_when_desk_writer_becomes_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Desk writer that grabs the lock during resolution defers just that batch.

    Mirrors ``test_run_venue_skips_db_processing_while_desk_writer_is_active``
    but for Phase A2 (see ``_run_resolution_phase``): backbone's own lock
    acquisition succeeds, but the resolution write batch's does not, so the
    venue stops before shot/PBP -- exactly like a shot/PBP batch deferral.
    """
    calls: list[str] = []
    deferred_reasons: list[str] = []
    _patch_backbone_services(
        monkeypatch,
        calls,
        resolution_pairs=[(object(), object())],
    )

    attempts = {"n": 0}

    async def _lock_busy_after_backbone(
        _db: object, _step_fields: object = None
    ) -> bool:
        attempts["n"] += 1
        return attempts["n"] == 1  # backbone succeeds; the resolution batch does not

    async def _defer(_db: object, *, reason: str) -> None:
        deferred_reasons.append(reason)

    monkeypatch.setattr(
        runner,
        "try_acquire_summer_league_writer_lock_yielding",
        _lock_busy_after_backbone,
    )
    monkeypatch.setattr(runner, "defer_full_reconciliation", _defer)
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),  # refresh
            _FakeManifest(game_ids=["001"]),  # fetch
        ]
    )

    result = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert result == (True, False)
    assert calls == ["backbone"]  # resolution deferred before shot/pbp ever ran
    assert deferred_reasons == ["venue:15:resolution_batch_lock_contended"]


@pytest.mark.asyncio
async def test_run_venue_with_games_backbone_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Games present but backbone failing returns (True, True)."""
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls, raise_in_backbone=True)
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),  # refresh
            _FakeManifest(game_ids=["001"]),  # fetch
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, True)
    assert calls == ["backbone"]  # failed before shot/pbp


@pytest.mark.asyncio
async def test_run_venue_batches_shot_and_pbp_normalization_by_game_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A venue with more games than the batch size normalizes shot/PBP in chunks.

    ``EVENT_BATCH_SIZE`` is 8; a 10-game venue should call
    ``normalize_shot_events``/``normalize_pbp_events`` twice each, with the
    game IDs partitioned into an 8-game batch then a 2-game batch --
    independently committed rather than one call covering the whole venue.
    """
    calls: list[str] = []
    shot_batches: list[set[str]] = []
    pbp_batches: list[set[str]] = []
    _patch_backbone_services(
        monkeypatch, calls, shot_batches=shot_batches, pbp_batches=pbp_batches
    )
    game_ids = [f"{i:03d}" for i in range(1, 11)]  # 10 games > batch size (8)
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=list(game_ids)),  # refresh
            _FakeManifest(game_ids=list(game_ids)),  # fetch
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert calls.count("shot") == 2
    assert calls.count("pbp") == 2
    assert shot_batches == [set(game_ids[:8]), set(game_ids[8:])]
    assert pbp_batches == [set(game_ids[:8]), set(game_ids[8:])]


@pytest.mark.asyncio
async def test_run_venue_skips_already_completed_batch_games(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Games already durably marked complete for a phase are excluded from its batches.

    Proves the resumability contract at the call-structure level: a game ID
    ``get_completed_batch_game_ids`` reports as already done for one phase
    never reappears in that phase's batch, while an independent phase with
    no completed games still gets the full set.
    """
    calls: list[str] = []
    shot_batches: list[set[str]] = []
    pbp_batches: list[set[str]] = []
    _patch_backbone_services(
        monkeypatch, calls, shot_batches=shot_batches, pbp_batches=pbp_batches
    )
    game_ids = ["001", "002", "003"]

    async def _completed(_db: object, *, phase: object, **_kwargs: object) -> set[str]:
        if phase == runner.SummerLeagueBatchPhase.SHOT:
            return {"001"}
        return set()

    monkeypatch.setattr(runner, "get_completed_batch_game_ids", _completed)

    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=list(game_ids)),
            _FakeManifest(game_ids=list(game_ids)),
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert shot_batches == [{"002", "003"}]
    assert pbp_batches == [{"001", "002", "003"}]


@pytest.mark.asyncio
async def test_run_venue_invalidates_dirty_shot_and_pbp_progress_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rewritten shotchartdetail/playbyplayv2 file invalidates only its own phase.

    Proves the endpoint-scoped dirty detection wired into `_run_venue`: game
    "001"'s rewritten shotchartdetail file invalidates its SHOT progress
    only; game "002"'s rewritten playbyplayv2 file invalidates its PBP
    progress only. A box-score-only rewrite (game "003") invalidates
    neither.
    """
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls)
    invalidate_calls: list[dict[str, object]] = []

    async def _record_invalidate(
        _db: object, *, phase: object, game_ids: object = None, **_kwargs: object
    ) -> None:
        invalidate_calls.append({"phase": phase, "game_ids": game_ids})

    monkeypatch.setattr(runner, "invalidate_batch_progress", _record_invalidate)

    game_ids = ["001", "002", "003"]
    fetch_manifest = _FakeManifest(
        game_ids=list(game_ids),
        files_written=[
            "2026/15/games/001/shotchartdetail.json",
            "2026/15/games/002/playbyplayv2.json",
            "2026/15/games/003/boxscoretraditionalv2.json",
        ],
    )
    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=list(game_ids)),  # refresh
            fetch_manifest,  # fetch
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert invalidate_calls == [
        {"phase": runner.SummerLeagueBatchPhase.SHOT, "game_ids": {"001"}},
        {"phase": runner.SummerLeagueBatchPhase.PBP, "game_ids": {"002"}},
    ]


@pytest.mark.asyncio
async def test_run_venue_no_dirty_files_invalidates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routine run with no rewritten per-game files never calls invalidation."""
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls)
    invalidate_calls: list[object] = []

    async def _record_invalidate(_db: object, **kwargs: object) -> None:
        invalidate_calls.append(kwargs)

    monkeypatch.setattr(runner, "invalidate_batch_progress", _record_invalidate)

    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),
            _FakeManifest(game_ids=["001"]),  # nothing in files_written
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (had_games, failed) == (True, False)
    assert invalidate_calls == []


@pytest.mark.asyncio
async def test_run_venue_full_reconcile_clears_all_progress_regardless_of_dirty_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """full_reconcile=True clears every SHOT/PBP row, ignoring dirty detection."""
    calls: list[str] = []
    _patch_backbone_services(monkeypatch, calls)
    invalidate_calls: list[dict[str, object]] = []

    async def _record_invalidate(
        _db: object, *, phase: object, game_ids: object = None, **_kwargs: object
    ) -> None:
        invalidate_calls.append({"phase": phase, "game_ids": game_ids})

    monkeypatch.setattr(runner, "invalidate_batch_progress", _record_invalidate)

    ingestor = _FakeIngestor(
        [
            _FakeManifest(game_ids=["001"]),
            _FakeManifest(game_ids=["001"]),  # no dirty files at all
        ]
    )

    had_games, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        ingestor,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
        full_reconcile=True,
    )

    assert (had_games, failed) == (True, False)
    assert invalidate_calls == [
        {"phase": runner.SummerLeagueBatchPhase.SHOT, "game_ids": None},
        {"phase": runner.SummerLeagueBatchPhase.PBP, "game_ids": None},
    ]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class _FakeClient:
    """Fake NBAStatsClient recording close()."""

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    venue_results: dict[str, tuple[bool, bool]],
    rebuild_raises: bool = False,
) -> dict[str, object]:
    """Wire up main() so it touches no real DB, network, or engine.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        venue_results: Map of league_id -> (had_games, failed) that the fake
            ``_run_venue`` should return per venue.
        rebuild_raises: When True, the fake metrics rebuild raises.

    Returns:
        A mutable ``events`` dict recording observable side effects: the
        venues processed, whether the rebuild ran, and whether the engine
        was disposed.
    """
    events: dict[str, object] = {
        "venues": [],
        "rebuild_called": False,
        "snapshots_refreshed": False,
        "disposed": False,
        "full_reconcile_flags": [],
    }

    async def _fake_run_venue(
        _db: object,
        _ingestor: object,
        *,
        year: int,
        league_id: str,
        telemetry: object | None = None,
        full_reconcile: bool = False,
    ) -> tuple[bool, bool]:
        assert telemetry is not None
        assert isinstance(events["venues"], list)
        events["venues"].append(league_id)
        assert isinstance(events["full_reconcile_flags"], list)
        events["full_reconcile_flags"].append(full_reconcile)
        return venue_results[league_id]

    async def _fake_rebuild(_db: object) -> dict[str, int]:
        events["rebuild_called"] = True
        if rebuild_raises:
            raise RuntimeError("rebuild boom")
        return {"seasons": 1, "contexts": 1, "adv_pools": 1}

    async def _fake_dispose() -> None:
        events["disposed"] = True

    async def _fake_materialize_snapshots(_db: object) -> int:
        events["snapshots_refreshed"] = True
        return 72

    async def _fake_refresh_schedule(
        _db: object, *, now: object, client: object
    ) -> None:
        events["schedule_refresh_called"] = True
        return None

    monkeypatch.setattr(runner, "_run_venue", _fake_run_venue)
    monkeypatch.setattr(runner, "rebuild_sl_metrics", _fake_rebuild)
    monkeypatch.setattr(
        runner, "materialize_desk_render_snapshots", _fake_materialize_snapshots
    )
    monkeypatch.setattr(runner, "dispose_engine", _fake_dispose)
    monkeypatch.setattr(runner, "NBAStatsClient", _FakeClient)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeSessionLocal())
    # Default every `main()` test to a no-op schedule refresh: this new step
    # queries the DB directly (`resolve_target_competitions`), which
    # `_FakeSession` doesn't support -- tests that specifically exercise the
    # wiring override this again below.
    monkeypatch.setattr(runner, "_refresh_schedule", _fake_refresh_schedule)
    events["schedule_refresh_called"] = False
    return events


class _FakeSessionLocal:
    """Async context manager standing in for ``SessionLocal()``."""

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_main_all_no_games_skips_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every venue no-games -> exit 0, metrics rebuild not called."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "13,16,15")
    events = _patch_main(
        monkeypatch,
        venue_results={
            "13": (False, False),
            "16": (False, False),
            "15": (False, False),
        },
    )

    result = await runner.main()

    assert result == 0
    assert events["venues"] == ["13", "16", "15"]
    assert events["rebuild_called"] is False
    assert events["snapshots_refreshed"] is False
    assert events["disposed"] is True
    assert events["full_reconcile_flags"] == [False, False, False]


@pytest.mark.asyncio
async def test_main_propagates_full_reconcile_env_var_to_every_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SL_INGEST_FULL_RECONCILE=1 is resolved once and passed to every venue."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "13,15")
    monkeypatch.setenv("SL_INGEST_FULL_RECONCILE", "1")
    events = _patch_main(
        monkeypatch,
        venue_results={"13": (False, False), "15": (True, False)},
    )

    result = await runner.main()

    assert result == 0
    assert events["full_reconcile_flags"] == [True, True]


@pytest.mark.asyncio
async def test_main_with_games_runs_rebuild_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At least one venue with games -> rebuild runs once, exit 0."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "13,15")
    events = _patch_main(
        monkeypatch,
        venue_results={"13": (False, False), "15": (True, False)},
    )

    result = await runner.main()

    assert result == 0
    assert events["rebuild_called"] is True
    assert events["snapshots_refreshed"] is True
    assert events["disposed"] is True


@pytest.mark.asyncio
async def test_main_drains_a_deferred_reconciliation_without_new_games(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior lock deferral forces the next full run to rebuild and refresh snapshots."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    events = _patch_main(monkeypatch, venue_results={"15": (False, False)})

    async def _pending(_db: object) -> bool:
        return True

    monkeypatch.setattr(runner, "full_reconciliation_is_pending", _pending)

    result = await runner.main()

    assert result == 0
    assert events["rebuild_called"] is True
    assert events["snapshots_refreshed"] is True


@pytest.mark.asyncio
async def test_main_venue_failure_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A venue reporting failed=True -> exit 1, but all venues attempted."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "13,15")
    events = _patch_main(
        monkeypatch,
        venue_results={"13": (True, True), "15": (True, False)},
    )

    result = await runner.main()

    assert result == 1
    assert events["venues"] == ["13", "15"]  # failure did not abort the rest
    assert events["disposed"] is True


@pytest.mark.asyncio
async def test_main_rebuild_failure_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics rebuild raising -> exit 1, engine still disposed."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    events = _patch_main(
        monkeypatch,
        venue_results={"15": (True, False)},
        rebuild_raises=True,
    )

    result = await runner.main()

    assert result == 1
    assert events["rebuild_called"] is True
    assert events["snapshots_refreshed"] is False
    assert events["disposed"] is True


# ---------------------------------------------------------------------------
# Competition Context (#618): environment refresh wiring inside main()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_calls_environment_refresh_after_snapshots_when_any_games(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A venue with games -> environment refresh runs once for the configured year."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    monkeypatch.setenv("SL_INGEST_YEAR", "2025")
    events = _patch_main(monkeypatch, venue_results={"15": (True, False)})

    calls: list[dict[str, object]] = []

    async def _recording_refresh(
        _db: object, *, year: int, telemetry: object | None = None
    ) -> _FakeEnvironmentRefreshOutcome:
        calls.append({"year": year, "telemetry": telemetry})
        return _FakeEnvironmentRefreshOutcome()

    monkeypatch.setattr(
        runner, "refresh_environment_profiles_for_year", _recording_refresh
    )

    result = await runner.main()

    assert result == 0
    assert events["rebuild_called"] is True
    assert events["snapshots_refreshed"] is True
    assert calls == [{"year": 2025, "telemetry": calls[0]["telemetry"]}]
    assert calls[0]["telemetry"] is not None


@pytest.mark.asyncio
async def test_main_skips_environment_refresh_when_no_games_and_nothing_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No games and no pending reconciliation -> environment refresh never runs.

    Mirrors the metrics/snapshot phase's own gate: a quiet cycle should
    leave the currently published Competition Context profiles untouched.
    """
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    events = _patch_main(monkeypatch, venue_results={"15": (False, False)})

    calls: list[object] = []

    async def _recording_refresh(_db: object, **_kwargs: object) -> object:
        calls.append(_kwargs)
        return _FakeEnvironmentRefreshOutcome()

    monkeypatch.setattr(
        runner, "refresh_environment_profiles_for_year", _recording_refresh
    )

    result = await runner.main()

    assert result == 0
    assert events["rebuild_called"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_main_environment_refresh_failure_does_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsuccessful (but non-raising) refresh outcome never flips the run to failed.

    `refresh_environment_profiles_for_year` isolates its own failures
    internally (never raises); this proves the runner doesn't second-guess
    that isolation by inspecting `outcome.succeeded` and failing the whole
    ingest run over it.
    """
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    events = _patch_main(monkeypatch, venue_results={"15": (True, False)})

    async def _failed_refresh(
        _db: object, *, year: int, telemetry: object | None = None
    ) -> _FakeEnvironmentRefreshOutcome:
        return _FakeEnvironmentRefreshOutcome(attempted=True, succeeded=False)

    monkeypatch.setattr(
        runner, "refresh_environment_profiles_for_year", _failed_refresh
    )

    result = await runner.main()

    assert result == 0
    assert events["rebuild_called"] is True
    assert events["snapshots_refreshed"] is True


@pytest.mark.asyncio
async def test_main_drains_deferred_reconciliation_runs_environment_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drained deferral (no new games this cycle) still triggers a refresh."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    monkeypatch.setenv("SL_INGEST_YEAR", "2026")
    events = _patch_main(monkeypatch, venue_results={"15": (False, False)})

    async def _pending(_db: object) -> bool:
        return True

    monkeypatch.setattr(runner, "full_reconciliation_is_pending", _pending)

    calls: list[dict[str, object]] = []

    async def _recording_refresh(
        _db: object, *, year: int, telemetry: object | None = None
    ) -> _FakeEnvironmentRefreshOutcome:
        calls.append({"year": year})
        return _FakeEnvironmentRefreshOutcome()

    monkeypatch.setattr(
        runner, "refresh_environment_profiles_for_year", _recording_refresh
    )

    result = await runner.main()

    assert result == 0
    assert events["rebuild_called"] is True
    assert calls == [{"year": 2026}]


@pytest.mark.asyncio
async def test_main_bad_year_returns_one_without_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid SL_INGEST_YEAR -> exit 1, no venue processed, engine disposed."""
    monkeypatch.setenv("SL_INGEST_YEAR", "26")
    events = _patch_main(monkeypatch, venue_results={})

    result = await runner.main()

    assert result == 1
    assert events["venues"] == []
    assert events["rebuild_called"] is False
    assert events["disposed"] is True


# ---------------------------------------------------------------------------
# _synthetic_schedule_dates
# ---------------------------------------------------------------------------


def _make_competition(
    *, starts_on: date | None, ends_on: date | None, year: int = 2026
) -> SummerLeagueCompetition:
    return SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug="las-vegas",
        display_name="2026 Las Vegas",
        starts_on=starts_on,
        ends_on=ends_on,
    )


def test_synthetic_schedule_dates_spans_inclusive_range() -> None:
    """A single competition's starts_on/ends_on expands to every day between."""
    comp = _make_competition(starts_on=date(2026, 7, 9), ends_on=date(2026, 7, 11))
    assert runner._synthetic_schedule_dates([comp]) == (
        date(2026, 7, 9),
        date(2026, 7, 10),
        date(2026, 7, 11),
    )


def test_synthetic_schedule_dates_skips_unconfigured_competitions() -> None:
    """A competition missing either date contributes nothing."""
    configured = _make_competition(starts_on=date(2026, 7, 9), ends_on=date(2026, 7, 9))
    unconfigured = _make_competition(starts_on=None, ends_on=None)
    assert runner._synthetic_schedule_dates([configured, unconfigured]) == (
        date(2026, 7, 9),
    )


def test_synthetic_schedule_dates_empty_for_no_competitions() -> None:
    """No competitions -> no dates."""
    assert runner._synthetic_schedule_dates([]) == ()


def test_synthetic_schedule_dates_skips_inverted_range() -> None:
    """A data-quality edge case (ends_on before starts_on) contributes nothing."""
    inverted = _make_competition(starts_on=date(2026, 7, 19), ends_on=date(2026, 7, 9))
    assert runner._synthetic_schedule_dates([inverted]) == ()


# ---------------------------------------------------------------------------
# _schedule_pull_in_window
# ---------------------------------------------------------------------------


def _patch_resolve_competitions(
    monkeypatch: pytest.MonkeyPatch, competitions: list[SummerLeagueCompetition]
) -> None:
    async def _fake_resolve(
        _db: object, *, today: date
    ) -> list[SummerLeagueCompetition]:
        return competitions

    monkeypatch.setattr(runner, "resolve_target_competitions", _fake_resolve)


@pytest.mark.asyncio
async def test_schedule_pull_in_window_true_during_active_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competition whose starts_on/ends_on span `now` -> in window (Active)."""
    _patch_resolve_competitions(
        monkeypatch,
        [_make_competition(starts_on=date(2026, 7, 9), ends_on=date(2026, 7, 19))],
    )
    now = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)

    assert await runner._schedule_pull_in_window(object(), now=now) is True


@pytest.mark.asyncio
async def test_schedule_pull_in_window_false_far_from_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competition scheduled months from now -> off-window (Dormant)."""
    _patch_resolve_competitions(
        monkeypatch,
        [_make_competition(starts_on=date(2026, 7, 9), ends_on=date(2026, 7, 19))],
    )
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    assert await runner._schedule_pull_in_window(object(), now=now) is False


@pytest.mark.asyncio
async def test_schedule_pull_in_window_false_no_competitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolvable competitions -> off-window, no lifecycle math attempted."""
    _patch_resolve_competitions(monkeypatch, [])
    now = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)

    assert await runner._schedule_pull_in_window(object(), now=now) is False


@pytest.mark.asyncio
async def test_schedule_pull_in_window_false_no_configured_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competition with no starts_on/ends_on yet (cold start) -> off-window.

    Mirrors `scripts/sl_desk_tick.py`'s `_needs_scoreboard_bootstrap` trade-off:
    with no real games and no configured window, there is no signal to reason
    about, so this stays network-free rather than guessing.
    """
    _patch_resolve_competitions(
        monkeypatch, [_make_competition(starts_on=None, ends_on=None)]
    )
    now = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)

    assert await runner._schedule_pull_in_window(object(), now=now) is False


@pytest.mark.asyncio
async def test_schedule_pull_in_window_true_during_winddown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A day after the last game (within post_roll_days) -> still in window."""
    _patch_resolve_competitions(
        monkeypatch,
        [_make_competition(starts_on=date(2026, 7, 9), ends_on=date(2026, 7, 19))],
    )
    now = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)  # 1 day past ends_on

    assert await runner._schedule_pull_in_window(object(), now=now) is True


# ---------------------------------------------------------------------------
# _refresh_schedule
# ---------------------------------------------------------------------------


@dataclass
class _FakeScoreboardReport:
    competitions_checked: int = 1
    games_seen: int = 11
    games_created: int = 5
    games_updated: int = 6
    errors: list[str] = field(default_factory=list)
    unresolved_team_ids: list[str] = field(default_factory=list)


@pytest.mark.asyncio
async def test_refresh_schedule_skips_when_out_of_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-window -> `run_scoreboard_ingest` is never called."""

    async def _fake_in_window(_db: object, *, now: object) -> bool:
        return False

    async def _fake_scoreboard_ingest(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("run_scoreboard_ingest should not be called off-window")

    monkeypatch.setattr(runner, "_schedule_pull_in_window", _fake_in_window)
    monkeypatch.setattr(runner, "run_scoreboard_ingest", _fake_scoreboard_ingest)

    result = await runner._refresh_schedule(
        _FakeSession(),  # type: ignore[arg-type]
        now=datetime(2026, 3, 1, tzinfo=timezone.utc),
        client=_FakeClient(),  # type: ignore[arg-type]
    )

    assert result is None


@pytest.mark.asyncio
async def test_refresh_schedule_calls_run_scoreboard_ingest_when_in_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-window -> `run_scoreboard_ingest` runs once with the shared client + today's ET date."""

    async def _fake_in_window(_db: object, *, now: object) -> bool:
        return True

    calls: list[dict[str, object]] = []
    fake_report = _FakeScoreboardReport()

    async def _fake_scoreboard_ingest(
        _db: object, *, today: object, client: object
    ) -> object:
        calls.append({"today": today, "client": client})
        return fake_report

    monkeypatch.setattr(runner, "_schedule_pull_in_window", _fake_in_window)
    monkeypatch.setattr(runner, "run_scoreboard_ingest", _fake_scoreboard_ingest)

    fake_client = _FakeClient()
    now = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)  # 2:00pm ET (EDT)
    result = await runner._refresh_schedule(
        _FakeSession(),  # type: ignore[arg-type]
        now=now,
        client=fake_client,  # type: ignore[arg-type]
    )

    assert result is fake_report
    assert calls == [{"today": date(2026, 7, 12), "client": fake_client}]


@pytest.mark.asyncio
async def test_refresh_schedule_swallows_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception (e.g. a DB error) is logged, not raised."""

    async def _raising_in_window(_db: object, *, now: object) -> bool:
        raise RuntimeError("db boom")

    monkeypatch.setattr(runner, "_schedule_pull_in_window", _raising_in_window)

    result = await runner._refresh_schedule(
        _FakeSession(),  # type: ignore[arg-type]
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
        client=_FakeClient(),  # type: ignore[arg-type]
    )

    assert result is None


# ---------------------------------------------------------------------------
# main() wiring for the schedule refresh step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_calls_refresh_schedule_with_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()` invokes the new schedule-refresh step, reusing the venue-loop client."""
    monkeypatch.setenv("SL_INGEST_LEAGUE_IDS", "15")
    events = _patch_main(monkeypatch, venue_results={"15": (False, False)})

    seen: dict[str, object] = {}

    async def _fake_refresh_schedule(
        _db: object, *, now: object, client: object
    ) -> None:
        seen["called"] = True
        seen["client"] = client
        return None

    monkeypatch.setattr(runner, "_refresh_schedule", _fake_refresh_schedule)

    result = await runner.main()

    assert result == 0
    assert seen["called"] is True
    assert isinstance(seen["client"], runner.NBAStatsClient)
    assert events["disposed"] is True

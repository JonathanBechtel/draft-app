"""Unit tests for the Summer League ingestion cron runner."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.cli import summer_league_ingest_runner as runner


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
# _run_venue
# ---------------------------------------------------------------------------


def _patch_backbone_services(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    raise_in_backbone: bool = False,
) -> None:
    """Monkeypatch the three DB-touching backbone/normalize services."""

    async def _fake_backbone(_db: object, _options: object) -> object:
        calls.append("backbone")
        if raise_in_backbone:
            raise RuntimeError("backbone boom")
        return object()

    async def _fake_shot(_db: object, **_kwargs: object) -> object:
        calls.append("shot")
        return _FakeShotReport()

    async def _fake_pbp(_db: object, **_kwargs: object) -> object:
        calls.append("pbp")
        return _FakePBPReport()

    monkeypatch.setattr(runner, "backfill_summer_league_backbone", _fake_backbone)
    monkeypatch.setattr(runner, "summarize_backfill_report", lambda _r: "summary")
    monkeypatch.setattr(runner, "normalize_shot_events", _fake_shot)
    monkeypatch.setattr(runner, "normalize_pbp_events", _fake_pbp)


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
    assert calls == ["backbone", "shot", "pbp"]


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
        "disposed": False,
    }

    async def _fake_run_venue(
        _db: object, _ingestor: object, *, year: int, league_id: str
    ) -> tuple[bool, bool]:
        assert isinstance(events["venues"], list)
        events["venues"].append(league_id)
        return venue_results[league_id]

    async def _fake_rebuild(_db: object) -> dict[str, int]:
        events["rebuild_called"] = True
        if rebuild_raises:
            raise RuntimeError("rebuild boom")
        return {"seasons": 1, "contexts": 1, "adv_pools": 1}

    async def _fake_dispose() -> None:
        events["disposed"] = True

    monkeypatch.setattr(runner, "_run_venue", _fake_run_venue)
    monkeypatch.setattr(runner, "rebuild_sl_metrics", _fake_rebuild)
    monkeypatch.setattr(runner, "dispose_engine", _fake_dispose)
    monkeypatch.setattr(runner, "NBAStatsClient", _FakeClient)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeSessionLocal())
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
    assert events["disposed"] is True


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
    assert events["disposed"] is True


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
    assert events["disposed"] is True


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

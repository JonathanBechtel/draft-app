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

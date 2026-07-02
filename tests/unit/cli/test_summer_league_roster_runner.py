"""Unit tests for the Summer League roster fetch + enrichment cron runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.cli import summer_league_roster_runner as runner


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


def _player_dict(
    person_id: str, team_id: str = "1", league_id: str = "15"
) -> dict[str, object]:
    """Build a minimal RosterEntry-shaped dict for a fake fetch result."""
    return {
        "nba_stats_person_id": person_id,
        "raw_player_name": f"Player {person_id}",
        "team_id": team_id,
        "jersey": "0",
        "position": "G",
        "height": "6-2",
        "weight": "190",
        "birth_date": "2000-01-01",
        "school": None,
        "how_acquired": "Draft",
        "league_id": league_id,
    }


@dataclass
class _FakeTeamResult:
    """Minimal stand-in for TeamFetchResult."""

    players: list[dict[str, object]] = field(default_factory=list)


@dataclass
class _FakeRosterRunResult:
    """Minimal stand-in for RosterRunResult."""

    team_count: int = 0
    player_count: int = 0
    error_count: int = 0
    error: str | None = None
    team_results: list[_FakeTeamResult] = field(default_factory=list)


class _FakeFetcher:
    """Fake RosterFetcher returning a queued result or raising."""

    def __init__(self, outcome: _FakeRosterRunResult | Exception) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, object]] = []

    def fetch_run(self, **kwargs: object) -> _FakeRosterRunResult:
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


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


class _FakeSessionLocal:
    """Async context manager standing in for ``SessionLocal()``."""

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@dataclass
class _FakeDiffReport:
    added: int = 1
    unchanged: int = 0
    cut: int = 0


@dataclass
class _FakeResolutionReport:
    total_source_players: int = 1
    resolved_source_players: int = 1
    unresolved_source_players: int = 0
    stubs_created: int = 0


@dataclass
class _FakeExternalIdReport:
    seeded: int = 1
    already_present: int = 0
    conflicts: list[object] = field(default_factory=list)


@dataclass
class _FakeHeadshotReport:
    set_count: int = 1
    skipped_existing: int = 0
    fallback: list[object] = field(default_factory=list)


@dataclass
class _FakeCollegeResult:
    players_attempted: int = 0
    players_scraped: int = 0
    players_skipped: int = 0
    players_failed: int = 0
    seasons_upserted: int = 0
    no_source: list[object] = field(default_factory=list)


def _patch_enrichment_steps(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    raise_in: str | None = None,
) -> None:
    """Monkeypatch every DB-touching enrichment step in the runner module."""

    async def _fake_load(*_args: object, **_kwargs: object) -> _FakeDiffReport:
        calls.append("load")
        if raise_in == "load":
            raise RuntimeError("load boom")
        return _FakeDiffReport()

    async def _fake_resolve(*_args: object, **_kwargs: object) -> _FakeResolutionReport:
        calls.append("resolve")
        if raise_in == "resolve":
            raise RuntimeError("resolve boom")
        return _FakeResolutionReport()

    async def _fake_seed(*_args: object, **_kwargs: object) -> _FakeExternalIdReport:
        calls.append("seed")
        if raise_in == "seed":
            raise RuntimeError("seed boom")
        return _FakeExternalIdReport()

    async def _fake_headshots(*_args: object, **_kwargs: object) -> _FakeHeadshotReport:
        calls.append("headshots")
        if raise_in == "headshots":
            raise RuntimeError("headshots boom")
        return _FakeHeadshotReport()

    async def _fake_bio(*_args: object, **_kwargs: object) -> None:
        calls.append("bio")
        if raise_in == "bio":
            raise RuntimeError("bio boom")

    async def _fake_college(*_args: object, **_kwargs: object) -> _FakeCollegeResult:
        calls.append("college")
        if raise_in == "college":
            raise RuntimeError("college boom")
        return _FakeCollegeResult()

    monkeypatch.setattr(runner, "load_roster_snapshot", _fake_load)
    monkeypatch.setattr(runner, "resolve_summer_league_players", _fake_resolve)
    monkeypatch.setattr(runner, "backfill_nba_stats_external_ids", _fake_seed)
    monkeypatch.setattr(runner, "backfill_nba_headshots", _fake_headshots)
    monkeypatch.setattr(runner, "_run_bio_enrichment", _fake_bio)
    monkeypatch.setattr(runner, "run_college_stats_sweep", _fake_college)


# ---------------------------------------------------------------------------
# _resolve_year
# ---------------------------------------------------------------------------


def test_resolve_year_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent env var yields the default Summer League year."""
    monkeypatch.delenv("SL_ROSTER_YEAR", raising=False)
    assert runner._resolve_year() == runner.DEFAULT_YEAR


def test_resolve_year_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SL_ROSTER_YEAR overrides the default year."""
    monkeypatch.setenv("SL_ROSTER_YEAR", "2025")
    assert runner._resolve_year() == 2025


def test_resolve_year_blank_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank/whitespace SL_ROSTER_YEAR falls back to the default."""
    monkeypatch.setenv("SL_ROSTER_YEAR", "   ")
    assert runner._resolve_year() == runner.DEFAULT_YEAR


@pytest.mark.parametrize("value", ["26", "20260", "abc"])
def test_resolve_year_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Non-four-digit or non-numeric SL_ROSTER_YEAR raises ValueError.

    This must fail up front so a misconfigured schedule exits non-zero rather
    than silently fetching nothing for every venue.
    """
    monkeypatch.setenv("SL_ROSTER_YEAR", value)
    with pytest.raises(ValueError):
        runner._resolve_year()


def test_resolve_year_valid_four_digit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid four-digit season year is accepted."""
    monkeypatch.setenv("SL_ROSTER_YEAR", "2025")
    assert runner._resolve_year() == 2025


# ---------------------------------------------------------------------------
# _resolve_league_ids
# ---------------------------------------------------------------------------


def test_resolve_league_ids_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent env var yields the default venue ordering (13, 16, 15)."""
    monkeypatch.delenv("SL_ROSTER_LEAGUE_IDS", raising=False)
    assert runner._resolve_league_ids() == ["13", "16", "15"]


def test_resolve_league_ids_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SL_ROSTER_LEAGUE_IDS overrides the venue list, preserving order."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", "15,13")
    assert runner._resolve_league_ids() == ["15", "13"]


def test_resolve_league_ids_dedup_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace is trimmed and duplicate LeagueIDs collapse to first-seen."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", " 15 , 13 ,15, 13 ")
    assert runner._resolve_league_ids() == ["15", "13"]


def test_resolve_league_ids_all_blank_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-blank SL_ROSTER_LEAGUE_IDS raises ValueError."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", " , , ")
    with pytest.raises(ValueError):
        runner._resolve_league_ids()


# ---------------------------------------------------------------------------
# _run_venue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_venue_fetch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw-fetch exception is folded into the not-published no-op path."""
    calls: list[str] = []
    _patch_enrichment_steps(monkeypatch, calls)
    fetcher = _FakeFetcher(RuntimeError("nba.com unreachable"))

    published, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        fetcher,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (published, failed) == (False, False)
    assert calls == []
    assert len(fetcher.calls) == 1


@pytest.mark.asyncio
async def test_run_venue_landing_error_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A landing-page error with zero teams is folded into not-published."""
    calls: list[str] = []
    _patch_enrichment_steps(monkeypatch, calls)
    fetcher = _FakeFetcher(_FakeRosterRunResult(error="landing 500", team_count=0))

    published, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        fetcher,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (published, failed) == (False, False)
    assert calls == []


@pytest.mark.asyncio
async def test_run_venue_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero published players skips enrichment without touching the DB."""
    calls: list[str] = []
    _patch_enrichment_steps(monkeypatch, calls)
    fetcher = _FakeFetcher(
        _FakeRosterRunResult(team_count=2, player_count=0, team_results=[])
    )

    published, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        fetcher,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (published, failed) == (False, False)
    assert calls == []


@pytest.mark.asyncio
async def test_run_venue_partial_team_fetch_failure_skips_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial team-fetch failure (error_count>0) skips the load entirely.

    Loading a partial snapshot would let the loader's empty-team cut wrongly
    CUT the roster of a team whose page transiently failed. So any team error
    skips the venue's load this run (retry next run) -- no DB writes, and it is
    not a run failure.
    """
    calls: list[str] = []
    _patch_enrichment_steps(monkeypatch, calls)
    team = _FakeTeamResult(players=[_player_dict("1"), _player_dict("2")])
    fetcher = _FakeFetcher(
        _FakeRosterRunResult(
            team_count=30, player_count=2, error_count=1, team_results=[team]
        )
    )

    published, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        fetcher,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (published, failed) == (False, False)
    assert calls == []


@pytest.mark.asyncio
async def test_run_venue_success_runs_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published roster runs every enrichment step in order."""
    calls: list[str] = []
    _patch_enrichment_steps(monkeypatch, calls)
    team = _FakeTeamResult(players=[_player_dict("1"), _player_dict("2")])
    fetcher = _FakeFetcher(
        _FakeRosterRunResult(
            team_count=1, player_count=2, error_count=0, team_results=[team]
        )
    )

    published, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        fetcher,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (published, failed) == (True, False)
    assert calls == ["load", "resolve", "seed", "headshots", "bio", "college"]


@pytest.mark.asyncio
async def test_run_venue_downstream_failure_stops_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolve failure marks the venue failed and skips later steps."""
    calls: list[str] = []
    _patch_enrichment_steps(monkeypatch, calls, raise_in="resolve")
    team = _FakeTeamResult(players=[_player_dict("1")])
    fetcher = _FakeFetcher(
        _FakeRosterRunResult(team_count=1, player_count=1, team_results=[team])
    )

    published, failed = await runner._run_venue(
        _FakeSession(),  # type: ignore[arg-type]
        fetcher,  # type: ignore[arg-type]
        year=2026,
        league_id="15",
    )

    assert (published, failed) == (True, True)
    assert calls == ["load", "resolve"]  # failed before seed/headshots/bio/college


# ---------------------------------------------------------------------------
# _run_bio_enrichment
# ---------------------------------------------------------------------------


@dataclass
class _FakeTargets:
    """Minimal stand-in for BioEnrichmentTargets."""

    slugs: set[str] = field(default_factory=set)
    manual_review_player_ids: set[int] = field(default_factory=set)


@pytest.mark.asyncio
async def test_run_bio_enrichment_no_targets_skips_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bbref-having cohort slugs skips scraping and ingest entirely."""
    scrape_calls: list[object] = []
    ingest_calls: list[object] = []

    async def _fake_targets(*_args: object, **_kwargs: object) -> _FakeTargets:
        return _FakeTargets(slugs=set(), manual_review_player_ids={1, 2})

    def _fake_scrape(**kwargs: object) -> list[dict[str, object]]:
        scrape_calls.append(kwargs)
        return []

    async def _fake_ingest(**kwargs: object) -> None:
        ingest_calls.append(kwargs)

    monkeypatch.setattr(runner, "select_bio_enrichment_targets", _fake_targets)
    monkeypatch.setattr(runner, "scrape_letters", _fake_scrape)
    monkeypatch.setattr(runner, "ingest_player_bios_csv", _fake_ingest)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeSessionLocal())

    await runner._run_bio_enrichment(year=2026, league_id="15")

    assert scrape_calls == []
    assert ingest_calls == []


@pytest.mark.asyncio
async def test_run_bio_enrichment_scrapes_and_ingests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bbref-having cohort slugs are scraped, written to CSV, then ingested."""
    ingest_calls: list[dict[str, object]] = []

    async def _fake_targets(*_args: object, **_kwargs: object) -> _FakeTargets:
        return _FakeTargets(slugs={"jamesle01"}, manual_review_player_ids=set())

    def _fake_scrape(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["extra_slugs"] == ["jamesle01"]
        return [{"slug": "jamesle01", "full_name": "LeBron James"}]

    async def _fake_ingest(**kwargs: object) -> None:
        ingest_calls.append(kwargs)

    monkeypatch.setattr(runner, "select_bio_enrichment_targets", _fake_targets)
    monkeypatch.setattr(runner, "scrape_letters", _fake_scrape)
    monkeypatch.setattr(runner, "ingest_player_bios_csv", _fake_ingest)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeSessionLocal())
    monkeypatch.setattr(runner, "BIO_OUT_DIR", tmp_path)

    await runner._run_bio_enrichment(year=2026, league_id="15")

    assert len(ingest_calls) == 1
    call = ingest_calls[0]
    assert call["summer_league_year"] == 2026
    assert call["summer_league_league_id"] == "15"
    assert call["create_missing"] is False
    written_csv = call["csv_path"]
    assert isinstance(written_csv, Path)
    assert written_csv.exists()
    assert written_csv.read_text(encoding="utf-8").splitlines()[0].startswith("slug,")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    venue_results: dict[str, tuple[bool, bool]],
) -> dict[str, object]:
    """Wire up main() so it touches no real DB, network, or engine.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        venue_results: Map of league_id -> (published, failed) that the fake
            ``_run_venue`` should return per venue.

    Returns:
        A mutable ``events`` dict recording observable side effects: the
        venues processed, whether schema modules were loaded, and whether
        the engine was disposed.
    """
    events: dict[str, object] = {
        "venues": [],
        "disposed": False,
        "schema_loaded": False,
    }

    async def _fake_run_venue(
        _db: object, _fetcher: object, *, year: int, league_id: str
    ) -> tuple[bool, bool]:
        assert isinstance(events["venues"], list)
        events["venues"].append(league_id)
        return venue_results[league_id]

    async def _fake_dispose() -> None:
        events["disposed"] = True

    def _fake_load_schema_modules() -> None:
        events["schema_loaded"] = True

    class _FakeRosterFetcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(runner, "_run_venue", _fake_run_venue)
    monkeypatch.setattr(runner, "dispose_engine", _fake_dispose)
    monkeypatch.setattr(runner, "load_schema_modules", _fake_load_schema_modules)
    monkeypatch.setattr(runner, "RosterFetcher", _FakeRosterFetcher)
    monkeypatch.setattr(runner, "SessionLocal", lambda: _FakeSessionLocal())
    return events


@pytest.mark.asyncio
async def test_main_all_not_published_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every venue not-published -> exit 0, all venues attempted."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", "13,16,15")
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
    assert events["disposed"] is True
    assert events["schema_loaded"] is True


@pytest.mark.asyncio
async def test_main_success_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """At least one venue published and clean -> exit 0."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", "13,15")
    events = _patch_main(
        monkeypatch, venue_results={"13": (False, False), "15": (True, False)}
    )

    result = await runner.main()

    assert result == 0
    assert events["disposed"] is True


@pytest.mark.asyncio
async def test_main_venue_failure_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A venue reporting failed=True -> exit 1, but all venues attempted."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", "13,15")
    events = _patch_main(
        monkeypatch, venue_results={"13": (True, True), "15": (True, False)}
    )

    result = await runner.main()

    assert result == 1
    assert events["venues"] == ["13", "15"]  # failure did not abort the rest
    assert events["disposed"] is True


@pytest.mark.asyncio
async def test_main_bad_year_returns_one_without_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid SL_ROSTER_YEAR -> exit 1, no venue processed, engine disposed."""
    monkeypatch.setenv("SL_ROSTER_YEAR", "26")
    events = _patch_main(monkeypatch, venue_results={})

    result = await runner.main()

    assert result == 1
    assert events["venues"] == []
    assert events["disposed"] is True
    assert events["schema_loaded"] is False


@pytest.mark.asyncio
async def test_main_bad_league_ids_returns_one_without_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-blank SL_ROSTER_LEAGUE_IDS -> exit 1, no venue processed."""
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", " , , ")
    events = _patch_main(monkeypatch, venue_results={})

    result = await runner.main()

    assert result == 1
    assert events["venues"] == []
    assert events["disposed"] is True

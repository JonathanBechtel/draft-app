"""Unit tests for the Summer League raw ingestion CLI."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from app.services.summer_league.manifest import SummerLeagueRawManifest
from app.services.summer_league.raw_ingestion import RawIngestionOptions
from scripts import fetch_summer_league_raw


class FakeIngestor:
    """Fake raw ingestor for CLI tests."""

    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.options: list[RawIngestionOptions] = []

    def fetch_year_league(
        self, options: RawIngestionOptions
    ) -> SummerLeagueRawManifest:
        """Record options and return a deterministic manifest."""
        self.options.append(options)
        if options.league_id in self.failures:
            raise RuntimeError(f"boom {options.league_id}")
        manifest = SummerLeagueRawManifest.start(
            year=options.year,
            league_id=options.league_id,
        )
        manifest.game_ids.extend(["G1", "G2"])
        manifest.files_written.extend(["leaguegamelog_team.json"])
        manifest.finish()
        return manifest


class FakeFactory:
    """Callable fake for build_ingestor."""

    def __init__(self, ingestor: FakeIngestor) -> None:
        self.ingestor = ingestor
        self.calls: list[tuple[float, Path, int, float, bool]] = []
        self.closed = False

    def __call__(
        self,
        *,
        timeout: float,
        out_dir: Path,
        retries: int,
        retry_delay: float,
        verbose: bool,
    ) -> object:
        """Return an IngestorContext-compatible object."""
        self.calls.append((timeout, out_dir, retries, retry_delay, verbose))
        return fetch_summer_league_raw.IngestorContext(
            ingestor=self.ingestor,  # type: ignore[arg-type]
            close=self.close,
        )

    def close(self) -> None:
        """Mark the fake context closed."""
        self.closed = True


def test_expand_league_ids_supports_repeated_and_comma_values() -> None:
    """League IDs can be repeated or comma-separated and are deduplicated."""
    assert fetch_summer_league_raw.expand_league_ids(["15,13", "15", "16"]) == [
        "15",
        "13",
        "16",
    ]


def test_expand_league_ids_rejects_invalid_values() -> None:
    """Unsupported LeagueIDs fail during argument validation."""
    with pytest.raises(ValueError, match="Unsupported Summer League"):
        fetch_summer_league_raw.expand_league_ids(["00"])


def test_expand_skip_endpoints_supports_repeated_and_comma_values() -> None:
    """Endpoint skips can be repeated or comma-separated and are deduplicated."""
    assert fetch_summer_league_raw.expand_skip_endpoints(
        ["playbyplayv2,shotchartdetail", "playbyplayv2"]
    ) == ("playbyplayv2", "shotchartdetail")


def test_expand_skip_endpoints_rejects_unknown_values() -> None:
    """Unknown endpoint names fail validation."""
    with pytest.raises(ValueError, match="Unsupported endpoint"):
        fetch_summer_league_raw.expand_skip_endpoints(["unknown"])


def test_parse_year_rejects_season_range() -> None:
    """Summer League years must be bare four-digit values."""
    with pytest.raises(SystemExit):
        fetch_summer_league_raw.main(["--year", "2024-25", "--league-id", "15"])


def test_main_passes_cli_options_to_ingestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful CLI runs construct options and print a compact summary."""
    ingestor = FakeIngestor()
    factory = FakeFactory(ingestor)
    monkeypatch.setattr(fetch_summer_league_raw, "build_ingestor", factory)

    exit_code = fetch_summer_league_raw.main(
        [
            "--year",
            "2024",
            "--league-id",
            "15,13",
            "--out-dir",
            str(tmp_path),
            "--timeout",
            "12",
            "--retries",
            "5",
            "--retry-delay",
            "1.5",
            "--delay",
            "0.2",
            "--limit-games",
            "1",
            "--force",
            "--dry-run",
            "--verbose",
            "--skip-endpoint",
            "playbyplayv2",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert factory.calls == [(12.0, tmp_path, 5, 1.5, True)]
    assert factory.closed is True
    assert [option.league_id for option in ingestor.options] == ["15", "13"]
    assert all(option.year == 2024 for option in ingestor.options)
    assert all(option.limit_games == 1 for option in ingestor.options)
    assert all(option.force is True for option in ingestor.options)
    assert all(option.dry_run is True for option in ingestor.options)
    assert all(option.delay_seconds == 0.2 for option in ingestor.options)
    assert all(option.skip_endpoints == ("playbyplayv2",) for option in ingestor.options)
    assert "2024 15 (las_vegas): games=2" in captured.out
    assert "2024 13 (california_classic): games=2" in captured.out


def test_main_returns_zero_for_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One failed LeagueID does not fail the whole command if another succeeds."""
    factory = FakeFactory(FakeIngestor(failures={"13"}))
    monkeypatch.setattr(fetch_summer_league_raw, "build_ingestor", factory)

    exit_code = fetch_summer_league_raw.main(
        ["--year", "2024", "--league-id", "15,13"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "2024 15 (las_vegas): games=2" in captured.out
    assert "2024 13: failed: RuntimeError: boom 13" in captured.err


def test_main_returns_one_when_all_requested_runs_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI exits non-zero when every requested LeagueID fails."""
    factory = FakeFactory(FakeIngestor(failures={"15", "13"}))
    monkeypatch.setattr(fetch_summer_league_raw, "build_ingestor", factory)

    exit_code = fetch_summer_league_raw.main(
        ["--year", "2024", "--league-id", "15,13"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "boom 15" in captured.err
    assert "boom 13" in captured.err


def test_main_rejects_negative_limit_games() -> None:
    """Negative limit-games values fail argparse validation."""
    argv: Sequence[str] = [
        "--year",
        "2024",
        "--league-id",
        "15",
        "--limit-games",
        "-1",
    ]
    with pytest.raises(SystemExit):
        fetch_summer_league_raw.main(argv)

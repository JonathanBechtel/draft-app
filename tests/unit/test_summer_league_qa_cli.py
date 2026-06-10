"""Unit tests for the Summer League QA CLI."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.summer_league.qa import (
    SummerLeagueQAFinding,
    SummerLeagueQAReport,
    SummerLeagueQASeverity,
    SummerLeagueSlice,
)
from scripts import qa_summer_league_backbone as qa_cli


class FakeEngine:
    """Minimal async engine stand-in for CLI tests."""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        """Mark the fake engine as disposed."""
        self.disposed = True


class FakeSessionContext:
    """Minimal async session context manager for CLI tests."""

    async def __aenter__(self) -> object:
        """Return a fake database session."""
        return object()

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        """Exit the fake session context."""


class FakeSessionFactory:
    """Callable fake matching async_sessionmaker output."""

    def __call__(self) -> FakeSessionContext:
        """Return a fake async session context."""
        return FakeSessionContext()


def _install_db_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeEngine:
    engine = FakeEngine()
    monkeypatch.setattr(qa_cli, "load_database_url", lambda: "postgresql+asyncpg://db")
    monkeypatch.setattr(qa_cli, "create_async_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        qa_cli,
        "async_sessionmaker",
        lambda *_args, **_kwargs: FakeSessionFactory(),
    )
    return engine


def test_build_slices_supports_repeated_comma_values_and_dedupes() -> None:
    """Slice parsing accepts repeated/comma values and preserves unique order."""
    slices = qa_cli.build_slices(
        year=2024,
        league_ids=["15,13", "15"],
        slice_values=["2023/15,2024/13"],
    )

    assert slices == [
        SummerLeagueSlice(2023, "15"),
        SummerLeagueSlice(2024, "13"),
        SummerLeagueSlice(2024, "15"),
    ]


def test_build_slices_requires_year_and_league_id_together() -> None:
    """Partial year/LeagueID arguments fail validation before DB access."""
    with pytest.raises(ValueError, match="provided together"):
        qa_cli.build_slices(year=2024, league_ids=None, slice_values=None)


def test_parse_slice_rejects_invalid_format() -> None:
    """Malformed slice values fail validation with a clear message."""
    with pytest.raises(ValueError, match="YEAR/LEAGUE_ID"):
        qa_cli.parse_slice("2024")


def test_render_markdown_report_includes_counts_failures_and_examples() -> None:
    """Markdown rendering includes scope, severity counts, failures, and evidence."""
    report = SummerLeagueQAReport(
        slices=(SummerLeagueSlice(2024, "15"),),
        findings=[
            SummerLeagueQAFinding(
                severity=SummerLeagueQASeverity.ERROR,
                code="RAW_FILE_MISSING",
                message="Expected raw file is missing.",
                evidence={"relative_path": "2024/15/missing.json"},
            ),
            SummerLeagueQAFinding(
                severity=SummerLeagueQASeverity.WARNING,
                code="PLAYER_UNRESOLVED",
                message="Player needs review.",
            ),
        ],
    )

    markdown = qa_cli.render_markdown_report(
        report,
        raw_root=Path("data/raw"),
        generated_at=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
    )

    assert "# Summer League Backbone QA Report" in markdown
    assert "- Status: `FAIL`" in markdown
    assert "- Slices: `2024/15`" in markdown
    assert "| error | 1 |" in markdown
    assert "| warning | 1 |" in markdown
    assert "| `RAW_FILE_MISSING` | 1 | Expected raw file is missing. |" in markdown
    assert 'Evidence: `{"relative_path": "2024/15/missing.json"}`' in markdown


def test_main_writes_report_and_returns_zero_for_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful QA output writes Markdown and exits zero."""
    engine = _install_db_fakes(monkeypatch)
    calls: list[list[SummerLeagueSlice]] = []

    async def fake_qa(
        _db: object,
        *,
        slices: list[SummerLeagueSlice],
    ) -> SummerLeagueQAReport:
        calls.append(slices)
        return SummerLeagueQAReport(slices=tuple(slices))

    monkeypatch.setattr(qa_cli, "run_summer_league_backbone_qa", fake_qa)
    report_path = tmp_path / "qa.md"

    exit_code = qa_cli.main(
        [
            "--year",
            "2024",
            "--league-id",
            "15",
            "--report-path",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == [[SummerLeagueSlice(2024, "15")]]
    assert engine.disposed is True
    assert report_path.read_text().startswith("# Summer League Backbone QA Report")
    assert "errors=0 warnings=0" in captured.out


def test_main_writes_report_and_returns_one_for_error_findings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Error-severity QA findings make the CLI exit non-zero."""
    _install_db_fakes(monkeypatch)

    async def fake_qa(
        _db: object,
        *,
        slices: list[SummerLeagueSlice],
    ) -> SummerLeagueQAReport:
        return SummerLeagueQAReport(
            slices=tuple(slices),
            findings=[
                SummerLeagueQAFinding(
                    severity=SummerLeagueQASeverity.ERROR,
                    code="NORMALIZATION_GAME_COUNT_MISMATCH",
                    message="Normalized game count does not match raw manifest.",
                )
            ],
        )

    monkeypatch.setattr(qa_cli, "run_summer_league_backbone_qa", fake_qa)
    report_path = tmp_path / "qa.md"

    exit_code = qa_cli.main(["--slice", "2024/15", "--report-path", str(report_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "errors=1 warnings=0" in captured.out
    assert "- Status: `FAIL`" in report_path.read_text()


def test_main_rejects_missing_slice_arguments() -> None:
    """The CLI requires at least one QA slice before opening the database."""
    argv: Sequence[str] = ["--raw-root", "data/raw"]
    with pytest.raises(SystemExit):
        qa_cli.main(argv)

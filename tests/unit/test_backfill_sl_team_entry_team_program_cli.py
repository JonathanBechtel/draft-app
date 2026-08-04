"""Unit coverage for the sl-team-entry team_program_id backfill CLI's pure logic."""

from __future__ import annotations

from scripts import backfill_sl_team_entry_team_program as cli


def test_build_parser_defaults() -> None:
    """A bare invocation means a full backfill with real writes."""
    args = cli.build_parser().parse_args([])
    assert args.dry_run is False
    assert args.database_url is None


def test_build_parser_accepts_dry_run_and_database_url() -> None:
    """Both operator flags parse as documented in the script's own signature."""
    args = cli.build_parser().parse_args(
        ["--dry-run", "--database-url", "postgresql+asyncpg://x/y"]
    )
    assert args.dry_run is True
    assert args.database_url == "postgresql+asyncpg://x/y"


def test_format_report_lines_reports_all_four_counts() -> None:
    """The one summary line names every measured count for ticket evidence."""
    report = cli.BackfillReport(eligible=10, updated=8, unresolvable=2, left_null=100)

    lines = cli.format_report_lines(report)

    assert len(lines) == 1
    assert "eligible=10" in lines[0]
    assert "updated=8" in lines[0]
    assert "unresolvable=2" in lines[0]
    assert "left_null=100" in lines[0]
    assert "(dry-run)" not in lines[0]


def test_format_report_lines_labels_dry_run() -> None:
    """Dry-run output is labelled so an estimate is never mistaken for a write."""
    report = cli.BackfillReport(eligible=5, updated=0)

    lines = cli.format_report_lines(report, dry_run=True)

    assert "(dry-run)" in lines[0]
    assert "eligible=5" in lines[0]
    assert "updated=0" in lines[0]


def test_backfill_report_defaults_are_all_zero() -> None:
    """A bare report (e.g. an empty table) reports zero everywhere."""
    report = cli.BackfillReport()
    assert (report.eligible, report.updated, report.unresolvable, report.left_null) == (
        0,
        0,
        0,
        0,
    )

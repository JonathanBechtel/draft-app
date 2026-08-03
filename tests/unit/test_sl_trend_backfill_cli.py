"""Unit coverage for the Summer League daily-trend backfill operator CLI."""

from __future__ import annotations

import argparse
from datetime import date
from typing import Any

import pytest

from scripts import backfill_sl_daily_trend_versions as cli


def test_max_backfill_year_tracks_the_current_eastern_year() -> None:
    """A future season is backfillable without editing a hardcoded constant."""
    assert cli.max_backfill_year(date(2029, 3, 4)) == 2029


def test_max_backfill_year_never_falls_below_the_minimum() -> None:
    """A nonsensical clock cannot invert the accepted year range."""
    assert cli.max_backfill_year(date(1999, 1, 1)) == cli.MIN_BACKFILL_YEAR


def test_valid_year_accepts_an_in_range_season() -> None:
    """The parser type coerces an in-range year string to an int."""
    assert cli._valid_year(str(cli.MIN_BACKFILL_YEAR)) == cli.MIN_BACKFILL_YEAR


@pytest.mark.parametrize("value", ["2016", "not-a-year", "3000"])
def test_valid_year_rejects_out_of_scope_values(value: str) -> None:
    """Pre-2017 seasons, future seasons, and junk all fail argument parsing."""
    with pytest.raises(argparse.ArgumentTypeError):
        cli._valid_year(value)


def test_build_parser_defaults() -> None:
    """A bare invocation means full sweep, real writes, default lock wait."""
    args = cli.build_parser().parse_args([])
    assert args.year is None
    assert args.dry_run is False
    assert args.lock_max_wait_seconds == cli.DEFAULT_LOCK_MAX_WAIT_SECONDS


def test_cumulative_player_counts_accumulates_through_day() -> None:
    """Each day's estimate is the distinct players seen on or before that day."""
    rows = [
        (7, date(2024, 7, 9), 1),
        (7, date(2024, 7, 9), 2),
        (7, date(2024, 7, 10), 2),
        (7, date(2024, 7, 10), 3),
        (8, date(2024, 7, 10), 9),
    ]

    counts = cli.cumulative_player_counts(rows)

    assert counts[(7, date(2024, 7, 9))] == 2
    assert counts[(7, date(2024, 7, 10))] == 3
    assert counts[(8, date(2024, 7, 10))] == 1


def test_cumulative_player_counts_is_order_independent() -> None:
    """Row order from the database cannot change the through-day estimates."""
    rows = [
        (7, date(2024, 7, 10), 3),
        (7, date(2024, 7, 9), 1),
        (7, date(2024, 7, 10), 1),
    ]

    counts = cli.cumulative_player_counts(rows)

    assert counts == {(7, date(2024, 7, 9)): 1, (7, date(2024, 7, 10)): 2}


def test_format_report_lines_omits_failure_block_when_clean() -> None:
    """A clean run prints one summary line and nothing else."""
    report = cli.BackfillReport(planned=3, archived=3, contexts=3, seasons=30)

    lines = cli.format_report_lines(report)

    assert len(lines) == 1
    assert "failed=0" in lines[0]
    assert "contexts=3" in lines[0]
    assert "est_contexts" not in lines[0]


def test_format_report_lines_labels_dry_run_counts_as_estimates() -> None:
    """Dry-run output is labelled so estimates are not mistaken for writes."""
    report = cli.BackfillReport(planned=2, pending=1, skipped=1, contexts=1, seasons=12)

    lines = cli.format_report_lines(report, dry_run=True)

    assert "(dry-run)" in lines[0]
    assert "pending=1" in lines[0]
    assert "est_contexts=1" in lines[0]
    assert "est_seasons=12" in lines[0]


def test_format_report_lines_enumerates_failed_targets() -> None:
    """Every skipped poison target is named in the summary for a retry."""
    target = cli.BackfillTarget(competition_id=5, year=2019, effective_day=date(2019, 7, 9))
    report = cli.BackfillReport(
        planned=2,
        archived=1,
        failures=[cli.BackfillFailure(target=target, error="RuntimeError: boom")],
    )

    lines = cli.format_report_lines(report)

    assert "failed=1" in lines[0]
    assert lines[1] == "FAILED TARGETS (1):"
    assert "competition=5" in lines[2]
    assert "effective_day=2019-07-09" in lines[2]
    assert "RuntimeError: boom" in lines[2]


class _RecordingSession:
    """Minimal AsyncSession stand-in that records rollbacks."""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        """Count a rollback issued by the runner."""
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_run_backfill_skips_poison_targets_and_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing target is recorded while later targets still archive."""
    targets = [
        cli.BackfillTarget(competition_id=1, year=2019, effective_day=date(2019, 7, 9)),
        cli.BackfillTarget(competition_id=2, year=2020, effective_day=date(2020, 7, 9)),
        cli.BackfillTarget(competition_id=3, year=2021, effective_day=date(2021, 7, 9)),
    ]

    async def fake_load_targets(db: Any, *, year: int | None = None) -> list[Any]:
        return targets

    async def fake_backfill_target(
        db: Any, target: cli.BackfillTarget, *, lock_max_wait_seconds: float
    ) -> Any:
        if target.competition_id == 1:
            raise RuntimeError("no complete projection")
        if target.competition_id == 2:
            raise cli._AlreadyArchived
        return cli.ArchivalPublication(contexts=1, seasons=4)

    monkeypatch.setattr(cli, "_load_targets", fake_load_targets)
    monkeypatch.setattr(cli, "_backfill_target", fake_backfill_target)
    session = _RecordingSession()

    report = await cli.run_backfill(session)  # type: ignore[arg-type]

    assert report.planned == 3
    assert report.archived == 1
    assert report.skipped == 1
    assert report.failed == 1
    assert report.contexts == 1
    assert report.seasons == 4
    assert report.failures[0].target == targets[0]
    assert "RuntimeError: no complete projection" == report.failures[0].error
    # One rollback ends the listing transaction, one recovers the poison target.
    assert session.rollbacks == 2


@pytest.mark.asyncio
async def test_run_backfill_rejects_a_non_positive_lock_wait() -> None:
    """An unbounded lock wait is refused at the boundary."""
    with pytest.raises(ValueError, match="lock_max_wait_seconds"):
        await cli.run_backfill(object(), lock_max_wait_seconds=0)  # type: ignore[arg-type]

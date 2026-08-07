"""Unit coverage for the multi-squad team_program population operator CLI (#810)."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest

from scripts import populate_multi_squad_team_programs as cli


def test_load_targets_returns_eight_stable_targets() -> None:
    """One target per multi-squad id, in a stable (sorted) order."""
    targets = cli._load_targets()

    assert len(targets) == 8
    assert [t.nba_stats_team_id for t in targets] == sorted(
        t.nba_stats_team_id for t in targets
    )


def test_load_targets_derive_the_expected_natural_keys() -> None:
    """Spot-check the Warriors Gold target's derived org/team_program slugs."""
    targets = {t.nba_stats_team_id: t for t in cli._load_targets()}

    gold = targets["1710612744"]
    assert gold.org_slug == "nba-warriors"
    assert gold.team_program_slug == "nba-warriors-gold"
    assert gold.name == "Golden State Warriors Gold"
    assert gold.level == "NBA-2"

    blue = targets["1810612744"]
    assert blue.team_program_slug == "nba-warriors-blue"
    assert blue.level == "NBA-3"


def test_pending_from_lookup_true_when_nothing_found() -> None:
    """A missing natural key means the row is pending creation."""
    assert cli.pending_from_lookup(None) is True


def test_pending_from_lookup_false_when_an_id_is_found() -> None:
    """An existing id means the row must be skipped, not recreated."""
    assert cli.pending_from_lookup(42) is False


def test_build_parser_defaults() -> None:
    """A bare invocation means a full population with real writes."""
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


def test_format_report_lines_omits_failure_block_when_clean() -> None:
    """A clean run prints one summary line and nothing else."""
    report = cli.PopulationReport(
        planned=8, team_programs_created=8, team_programs_skipped=0
    )

    lines = cli.format_report_lines(report)

    assert len(lines) == 1
    assert "failed=0" in lines[0]
    assert "team_programs_created=8" in lines[0]
    assert "organization_missing=0" in lines[0]
    assert "(dry-run)" not in lines[0]


def test_format_report_lines_labels_dry_run() -> None:
    """Dry-run output is labelled so an estimate is never mistaken for a write."""
    report = cli.PopulationReport(
        planned=8, team_programs_created=3, team_programs_skipped=5
    )

    lines = cli.format_report_lines(report, dry_run=True)

    assert "(dry-run)" in lines[0]
    assert "team_programs_created=3" in lines[0]
    assert "team_programs_skipped=5" in lines[0]


def test_format_report_lines_enumerates_failed_targets() -> None:
    """Every skipped poison target is named in the summary for a retry."""
    target = cli.PopulationTarget(
        nba_stats_team_id="1710612744",
        org_slug="nba-warriors",
        team_program_slug="nba-warriors-gold",
        name="Golden State Warriors Gold",
        level="NBA-2",
    )
    report = cli.PopulationReport(
        planned=1,
        failures=[cli.PopulationFailure(target=target, error="RuntimeError: boom")],
    )

    lines = cli.format_report_lines(report)

    assert "failed=1" in lines[0]
    assert lines[1] == "FAILED TARGETS (1):"
    assert "nba_stats_team_id=1710612744" in lines[2]
    assert "RuntimeError: boom" in lines[2]


class _RecordingSession:
    """Minimal AsyncSession stand-in that records rollbacks."""

    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        """Count a rollback issued by the runner."""
        self.rollbacks += 1

    def begin(self) -> "_FakeTransaction":
        """Return a no-op transaction scope."""
        return _FakeTransaction()


class _FakeTransaction:
    """Stand-in for the transaction context ``AsyncSession.begin`` yields."""

    async def __aenter__(self) -> None:
        """Enter the fake transaction."""
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Never suppress; a failed target must propagate to the runner."""
        return False


@pytest.mark.asyncio
async def test_run_population_reports_organization_missing_separately_from_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target whose parent organization is missing is not a generic failure.

    T3 (populate_org_model_from_nba_teams.py) may simply not have run yet for
    a franchise -- that is an expected, reportable state (run it first), not
    a poison target that belongs in the failures list.
    """
    targets = [
        cli.PopulationTarget(
            nba_stats_team_id="1710612744",
            org_slug="nba-warriors",
            team_program_slug="nba-warriors-gold",
            name="Golden State Warriors Gold",
            level="NBA-2",
        ),
        cli.PopulationTarget(
            nba_stats_team_id="1810612744",
            org_slug="nba-warriors",
            team_program_slug="nba-warriors-blue",
            name="Golden State Warriors Blue",
            level="NBA-3",
        ),
    ]

    async def fake_populate_target(db: Any, target: cli.PopulationTarget) -> bool:
        if target.nba_stats_team_id == "1710612744":
            raise cli._OrganizationNotPopulatedError("organization missing")
        return True

    monkeypatch.setattr(cli, "_load_targets", lambda: targets)
    monkeypatch.setattr(cli, "_populate_target", fake_populate_target)
    session = _RecordingSession()

    report = await cli.run_population(session)  # type: ignore[arg-type]

    assert report.planned == 2
    assert report.organization_missing == 1
    assert report.team_programs_created == 1
    assert report.failed == 0


@pytest.mark.asyncio
async def test_run_population_skips_poison_targets_and_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely failing target is recorded while later targets still populate."""
    targets = [
        cli.PopulationTarget(
            nba_stats_team_id="1710612744",
            org_slug="nba-warriors",
            team_program_slug="nba-warriors-gold",
            name="Golden State Warriors Gold",
            level="NBA-2",
        ),
        cli.PopulationTarget(
            nba_stats_team_id="1810612744",
            org_slug="nba-warriors",
            team_program_slug="nba-warriors-blue",
            name="Golden State Warriors Blue",
            level="NBA-3",
        ),
    ]

    async def fake_populate_target(db: Any, target: cli.PopulationTarget) -> bool:
        if target.nba_stats_team_id == "1710612744":
            raise RuntimeError("slug collision")
        return True

    monkeypatch.setattr(cli, "_load_targets", lambda: targets)
    monkeypatch.setattr(cli, "_populate_target", fake_populate_target)
    session = _RecordingSession()

    report = await cli.run_population(session)  # type: ignore[arg-type]

    assert report.planned == 2
    assert report.failed == 1
    assert report.failures[0].target == targets[0]
    assert "RuntimeError: slug collision" == report.failures[0].error
    assert report.team_programs_created == 1
    assert report.organization_missing == 0


@pytest.mark.asyncio
async def test_run_population_dry_run_reports_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run probes existing rows and never calls the writer path."""
    targets = [
        cli.PopulationTarget(
            nba_stats_team_id="1710612744",
            org_slug="nba-warriors",
            team_program_slug="nba-warriors-gold",
            name="Golden State Warriors Gold",
            level="NBA-2",
        ),
        cli.PopulationTarget(
            nba_stats_team_id="1710612753",
            org_slug="nba-magic",
            team_program_slug="nba-magic-white",
            name="Orlando Magic White",
            level="NBA-2",
        ),
    ]
    existing_orgs = {"nba-warriors": 900, "nba-magic": 901}
    existing_programs = {"nba-magic-white": 44}

    async def fake_find_org(db: Any, slug: str) -> int | None:
        return existing_orgs.get(slug)

    async def fake_find_program(db: Any, slug: str) -> int | None:
        return existing_programs.get(slug)

    def fail_populate_target(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry-run must never call the writer path")

    monkeypatch.setattr(cli, "_load_targets", lambda: targets)
    monkeypatch.setattr(cli, "_find_organization_id", fake_find_org)
    monkeypatch.setattr(cli, "_find_team_program_id", fake_find_program)
    monkeypatch.setattr(cli, "_populate_target", fail_populate_target)

    report = await cli.run_population(object(), dry_run=True)  # type: ignore[arg-type]

    assert report.planned == 2
    assert report.team_programs_created == 1  # Warriors Gold pending
    assert report.team_programs_skipped == 1  # Magic White exists
    assert report.organization_missing == 0

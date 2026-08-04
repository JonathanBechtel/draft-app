"""Unit coverage for the org-model-from-nba_teams population operator CLI."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest

from scripts import populate_org_model_from_nba_teams as cli


def test_derive_org_slug_is_prefixed_with_the_nba_namespace() -> None:
    """The org slug is derived from the immutable nba_teams.slug, not the name."""
    assert cli.derive_org_slug("lakers") == "nba-lakers"


def test_derive_team_program_slug_matches_the_organization_slug() -> None:
    """One program per org this ticket creates -- the natural key is the same value."""
    assert cli.derive_team_program_slug("celtics") == cli.derive_org_slug("celtics")
    assert cli.derive_team_program_slug("celtics") == "nba-celtics"


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
        planned=30,
        organizations_created=30,
        team_programs_created=30,
    )

    lines = cli.format_report_lines(report)

    assert len(lines) == 1
    assert "failed=0" in lines[0]
    assert "organizations_created=30" in lines[0]
    assert "team_programs_created=30" in lines[0]
    assert "(dry-run)" not in lines[0]


def test_format_report_lines_labels_dry_run() -> None:
    """Dry-run output is labelled so an estimate is never mistaken for a write."""
    report = cli.PopulationReport(
        planned=30, organizations_created=5, organizations_skipped=25
    )

    lines = cli.format_report_lines(report, dry_run=True)

    assert "(dry-run)" in lines[0]
    assert "organizations_created=5" in lines[0]
    assert "organizations_skipped=25" in lines[0]


def test_format_report_lines_enumerates_failed_targets() -> None:
    """Every skipped poison target is named in the summary for a retry."""
    target = cli.PopulationTarget(
        nba_team_id=7, name="Test Team", org_slug="nba-test", team_program_slug="nba-test"
    )
    report = cli.PopulationReport(
        planned=2,
        organizations_created=1,
        team_programs_created=1,
        failures=[cli.PopulationFailure(target=target, error="RuntimeError: boom")],
    )

    lines = cli.format_report_lines(report)

    assert "failed=1" in lines[0]
    assert lines[1] == "FAILED TARGETS (1):"
    assert "nba_team_id=7" in lines[2]
    assert "Test Team" in lines[2]
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
async def test_run_population_skips_poison_targets_and_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing target is recorded while later targets still populate."""
    targets = [
        cli.PopulationTarget(nba_team_id=1, name="A", org_slug="nba-a", team_program_slug="nba-a"),
        cli.PopulationTarget(nba_team_id=2, name="B", org_slug="nba-b", team_program_slug="nba-b"),
        cli.PopulationTarget(nba_team_id=3, name="C", org_slug="nba-c", team_program_slug="nba-c"),
    ]

    async def fake_load_targets(db: Any) -> list[Any]:
        return targets

    async def fake_populate_target(db: Any, target: cli.PopulationTarget) -> tuple[bool, bool]:
        if target.nba_team_id == 1:
            raise RuntimeError("slug collision")
        if target.nba_team_id == 2:
            return (False, False)
        return (True, True)

    monkeypatch.setattr(cli, "_load_targets", fake_load_targets)
    monkeypatch.setattr(cli, "_populate_target", fake_populate_target)
    session = _RecordingSession()

    report = await cli.run_population(session)  # type: ignore[arg-type]

    assert report.planned == 3
    assert report.organizations_created == 1
    assert report.organizations_skipped == 1
    assert report.team_programs_created == 1
    assert report.team_programs_skipped == 1
    assert report.failed == 1
    assert report.failures[0].target == targets[0]
    assert "RuntimeError: slug collision" == report.failures[0].error
    # One rollback ends the listing transaction, one recovers the poison target.
    assert session.rollbacks == 2


@pytest.mark.asyncio
async def test_run_population_dry_run_reports_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run probes existing rows and never calls the writer path."""
    targets = [
        cli.PopulationTarget(nba_team_id=1, name="A", org_slug="nba-a", team_program_slug="nba-a"),
        cli.PopulationTarget(nba_team_id=2, name="B", org_slug="nba-b", team_program_slug="nba-b"),
    ]
    existing_orgs = {"nba-b": 99}
    existing_programs: dict[str, int] = {}

    async def fake_load_targets(db: Any) -> list[Any]:
        return targets

    async def fake_find_org(db: Any, slug: str) -> int | None:
        return existing_orgs.get(slug)

    async def fake_find_program(db: Any, slug: str) -> int | None:
        return existing_programs.get(slug)

    def fail_populate_target(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry-run must never call the writer path")

    monkeypatch.setattr(cli, "_load_targets", fake_load_targets)
    monkeypatch.setattr(cli, "_find_organization_id", fake_find_org)
    monkeypatch.setattr(cli, "_find_team_program_id", fake_find_program)
    monkeypatch.setattr(cli, "_populate_target", fail_populate_target)

    report = await cli.run_population(object(), dry_run=True)  # type: ignore[arg-type]

    assert report.planned == 2
    assert report.organizations_created == 1  # team A pending
    assert report.organizations_skipped == 1  # team B exists
    assert report.team_programs_created == 2  # neither program exists yet
    assert report.team_programs_skipped == 0
    assert report.failed == 0

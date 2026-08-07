"""Populate the organization / team_program model from ``nba_teams``.

Per journey-graph alignment §3 and phase-4 spec §5.1 (decision D2), the NBA
franchise set is the deliberate first population of the new org model: it is
closed, known, and correct, so the model ships *validated by an existing
production spoke* rather than as a speculative schema. For each ``nba_teams``
row this creates:

- one ``Organization`` of kind ``CLUB``
- one ``TeamProgram`` (level ``"NBA"``) owned by that organization

Nothing here links a ``team_program`` back to an SL team entry, and nothing
here populates ``organization_relationship`` rows -- both are later tickets.

Idempotent -- keyed on a slug derived from the immutable ``nba_teams.slug``
(never on row-insertion order), so re-running creates nothing and reports
zero pending. Each target (one NBA franchise) is independent: a target that
fails is skipped and reported rather than aborting the run, and the process
exits non-zero when any target failed.

Run (dev first; never point this at production without review):

  scripts/with-db-env.sh conda run -n draftguru python scripts/populate_org_model_from_nba_teams.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python scripts/populate_org_model_from_nba_teams.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402
from app.schemas.nba_teams import NbaTeam  # noqa: E402
from app.schemas.organization import Organization, OrgKind, TeamProgram  # noqa: E402

# Re-exported for tests/unit/test_team_program_resolution.py, which asserts
# byte-identical output against the backbone copy. The canonical
# implementation lives in app/services/backbone/ (ticket #796) so ingest can
# also use it -- app/ cannot import scripts/, so the natural key had to move
# into the shipped package rather than stay script-only. The two
# scripts/backfill_* scripts (#799) get it indirectly, via
# scripts/_franchise_team_program_map.py importing straight from the
# backbone module rather than through this re-export.
from app.services.backbone.team_program_resolution import (  # noqa: E402,F401
    derive_org_slug,
)
from app.utils.db_async import _prepare_asyncpg_connection  # noqa: E402

TEAM_PROGRAM_LEVEL = "NBA"


def derive_team_program_slug(nba_team_slug: str) -> str:
    """Return the stable ``team_programs.slug`` for one NBA franchise's roster.

    This population creates exactly one program per organization, so the
    program reuses the organization's slug value -- the two tables enforce
    uniqueness independently, so there is no collision between them.

    Args:
        nba_team_slug: The immutable ``nba_teams.slug`` value (e.g. ``"lakers"``).

    Returns:
        The natural key this script keys idempotency on (e.g. ``"nba-lakers"``).
    """
    return derive_org_slug(nba_team_slug)


@dataclass(frozen=True)
class PopulationTarget:
    """One ``nba_teams`` row to convert into an organization + team_program."""

    nba_team_id: int
    name: str
    org_slug: str
    team_program_slug: str


@dataclass(frozen=True)
class PopulationFailure:
    """One target that could not be populated, retained for the run summary."""

    target: PopulationTarget
    error: str


@dataclass
class PopulationReport:
    """Measured operator counts suitable for dry-run and ticket evidence.

    In a real run the ``*_created``/``*_skipped`` counts reflect what was
    actually written. In a dry run they are the probe-aware counts of what
    *would* be created or skipped, so the same numbers are comparable
    before and after the real run.
    """

    planned: int = 0
    organizations_created: int = 0
    organizations_skipped: int = 0
    team_programs_created: int = 0
    team_programs_skipped: int = 0
    failures: list[PopulationFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Return the number of targets that raised and were skipped."""
        return len(self.failures)


async def _load_targets(db: AsyncSession) -> list[PopulationTarget]:
    """Return one population target per ``nba_teams`` row, in a stable order."""
    query = select(NbaTeam.id, NbaTeam.name, NbaTeam.slug).order_by(NbaTeam.id)  # type: ignore[call-overload]
    rows = (await db.execute(query)).all()
    return [
        PopulationTarget(
            nba_team_id=int(team_id),
            name=name,
            org_slug=derive_org_slug(team_slug),
            team_program_slug=derive_team_program_slug(team_slug),
        )
        for team_id, name, team_slug in rows
    ]


def pending_from_lookup(existing_id: int | None) -> bool:
    """Return whether a row must be created, given its existing-id lookup result.

    Isolated as a pure function so the create/skip decision is unit-testable
    without a database: ``None`` (nothing found for the natural key) means the
    row is pending, any id means it already exists and must be skipped.

    Args:
        existing_id: The id returned by a slug lookup, or ``None`` if absent.

    Returns:
        ``True`` when the row is pending creation.
    """
    return existing_id is None


async def _find_organization_id(db: AsyncSession, slug: str) -> int | None:
    """Return the id of an existing organization with ``slug``, if any."""
    return await db.scalar(select(Organization.id).where(Organization.slug == slug))  # type: ignore[call-overload,arg-type]


async def _find_team_program_id(db: AsyncSession, slug: str) -> int | None:
    """Return the id of an existing team_program with ``slug``, if any."""
    return await db.scalar(select(TeamProgram.id).where(TeamProgram.slug == slug))  # type: ignore[call-overload,arg-type]


async def _populate_target(
    db: AsyncSession, target: PopulationTarget
) -> tuple[bool, bool]:
    """Create the organization and/or team_program for one target if missing.

    Returns:
        ``(organization_created, team_program_created)``.
    """
    organization_id = await _find_organization_id(db, target.org_slug)
    organization_created = pending_from_lookup(organization_id)
    if organization_created:
        organization = Organization(
            org_kind=OrgKind.CLUB, name=target.name, slug=target.org_slug
        )
        db.add(organization)
        await db.flush()
        assert organization.id is not None
        organization_id = organization.id

    team_program_id = await _find_team_program_id(db, target.team_program_slug)
    team_program_created = pending_from_lookup(team_program_id)
    if team_program_created:
        program = TeamProgram(
            organization_id=organization_id,
            name=target.name,
            slug=target.team_program_slug,
            level=TEAM_PROGRAM_LEVEL,
        )
        db.add(program)
        await db.flush()

    return organization_created, team_program_created


async def _plan_dry_run(
    db: AsyncSession, targets: Sequence[PopulationTarget]
) -> PopulationReport:
    """Probe every target and report pending work without writing anything."""
    report = PopulationReport(planned=len(targets))
    for target in targets:
        organization_id = await _find_organization_id(db, target.org_slug)
        organization_pending = pending_from_lookup(organization_id)
        if organization_pending:
            report.organizations_created += 1
        else:
            report.organizations_skipped += 1

        team_program_id = await _find_team_program_id(db, target.team_program_slug)
        team_program_pending = pending_from_lookup(team_program_id)
        if team_program_pending:
            report.team_programs_created += 1
        else:
            report.team_programs_skipped += 1

        print(
            f"DRY-RUN nba_team_id={target.nba_team_id} name={target.name!r} "
            f"org={'pending' if organization_pending else 'exists'} "
            f"team_program={'pending' if team_program_pending else 'exists'}"
        )
    return report


async def run_population(
    db: AsyncSession, *, dry_run: bool = False
) -> PopulationReport:
    """Run the org-model population and return measured counts.

    Targets are independent. A target that raises is recorded in
    ``PopulationReport.failures`` and the run continues, so one bad row
    cannot strand the rest of the population.

    Args:
        db: Active database session; the function owns its own transactions.
        dry_run: Probe targets and report pending counts without writing.

    Returns:
        Measured counts, including per-target failures.
    """
    targets = await _load_targets(db)
    if dry_run:
        return await _plan_dry_run(db, targets)

    report = PopulationReport(planned=len(targets))
    # The target listing opens a read transaction on AsyncSession. End it
    # before entering the per-target transaction so a failed target can roll
    # back cleanly without disturbing the ones that already succeeded.
    await db.rollback()
    for target in targets:
        try:
            async with db.begin():
                organization_created, team_program_created = await _populate_target(
                    db, target
                )
        except Exception as exc:  # noqa: BLE001 - one bad target must not abort the run
            with contextlib.suppress(Exception):
                await db.rollback()
            report.failures.append(
                PopulationFailure(target=target, error=f"{type(exc).__name__}: {exc}")
            )
            continue

        if organization_created:
            report.organizations_created += 1
        else:
            report.organizations_skipped += 1
        if team_program_created:
            report.team_programs_created += 1
        else:
            report.team_programs_skipped += 1

    return report


def format_report_lines(
    report: PopulationReport, *, dry_run: bool = False
) -> list[str]:
    """Render the operator summary, including the per-target failure list."""
    label = "org model population from nba_teams" + (" (dry-run)" if dry_run else "")
    lines = [
        f"{label}: planned={report.planned} "
        f"organizations_created={report.organizations_created} "
        f"organizations_skipped={report.organizations_skipped} "
        f"team_programs_created={report.team_programs_created} "
        f"team_programs_skipped={report.team_programs_skipped} "
        f"failed={report.failed}"
    ]
    if report.failures:
        lines.append(f"FAILED TARGETS ({report.failed}):")
        lines.extend(
            f"  nba_team_id={failure.target.nba_team_id} "
            f"name={failure.target.name!r} error={failure.error}"
            for failure in report.failures
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "probe each nba_teams row for an existing organization/team_program "
            "and report the pending counts, without writing"
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Defaults to DATABASE_URL / the app's configured database.",
    )
    return parser


async def _run(*, dry_run: bool, database_url: str) -> int:
    """Open a session against ``database_url`` and run the population."""
    normalized_url, connect_args = _prepare_asyncpg_connection(database_url)
    engine = create_async_engine(normalized_url, echo=False, connect_args=connect_args)
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with session_factory() as db:
            report = await run_population(db, dry_run=dry_run)
    finally:
        # Always release the pool, including when target discovery itself raised.
        await engine.dispose()

    for line in format_report_lines(report, dry_run=dry_run):
        print(line)
    return 1 if report.failures else 0


async def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point; returns a non-zero status when any target failed."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    database_url = (
        args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    )
    return await _run(dry_run=args.dry_run, database_url=database_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

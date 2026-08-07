"""Populate the second/third Summer League squad ``TeamProgram`` rows (#810).

Four NBA franchises field more than one Summer League squad in the same
competition edition -- Golden State Warriors (Gold/Blue), Orlando Magic
(White/Blue), Sacramento Kings (Kings 1/Kings 2), and Utah Jazz (White/Blue).
``stats.nba.com`` encodes each sibling squad by swapping the standard ``16``
team-id prefix for ``17`` (second squad) / ``18`` (third squad) while keeping
the franchise's 9-digit suffix intact. See
``app.services.backbone.team_program_resolution``'s module docstring
("Multi-squad franchises") for the full id table and the design rationale --
in short, these are modelled as **additional** ``TeamProgram`` rows under the
franchise's existing ``Organization``, never collapsed onto its primary
program, because the sibling squads play *against* each other in the same
competition edition.

This is a deliberate sibling to ``scripts/populate_org_model_from_nba_teams.py``
(T3) rather than a change folded into it: T3's ``run_population`` is exercised
directly (not just via its CLI) by ``tests/integration/test_team_program_resolution_ingest.py``,
which asserts exactly one ``team_programs`` row per seeded franchise -- folding
multi-squad creation into that function would make it return more rows for
the four affected franchises and break that assertion for a case those tests
don't (and shouldn't need to) know about. Keeping the two population steps
separate also mirrors the two-strategy split already used by
``scripts/backfill_sl_team_entry_team_program.py``: this script is strictly
additive and depends on T3 having already created the parent organization.

Idempotent -- keyed on the sibling program's own natural-key slug
(``derive_org_slug``'s ``"nba-"`` prefix + the squad's ``slug_suffix``, e.g.
``"nba-warriors-gold"``), so re-running creates nothing and reports zero
pending. Each target is independent: a target whose parent organization has
not been populated yet (T3 hasn't run for that franchise) is skipped and
reported rather than aborting the run.

Run (dev first; never point this at production without review; run
``scripts/populate_org_model_from_nba_teams.py`` first so the parent
organizations exist):

  scripts/with-db-env.sh conda run -n draftguru python scripts/populate_multi_squad_team_programs.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python scripts/populate_multi_squad_team_programs.py
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
from app.schemas.organization import Organization, TeamProgram  # noqa: E402
from app.services.backbone.team_program_resolution import (  # noqa: E402
    NBA_STATS_MULTI_SQUAD_TEAM_IDS,
    ORG_SLUG_PREFIX,
    MultiSquadTeamProgram,
    derive_org_slug,
)
from app.utils.db_async import _prepare_asyncpg_connection  # noqa: E402


def derive_multi_squad_team_program_slug(multi_squad: MultiSquadTeamProgram) -> str:
    """Return the stable ``team_programs.slug`` for one sibling squad.

    Uses the same ``ORG_SLUG_PREFIX`` namespace :func:`derive_org_slug` and
    T3's ``derive_team_program_slug`` use, so all ``team_programs`` slugs stay
    in one recognizable family, but appends the squad's own suffix (e.g.
    ``"warriors-gold"``) rather than reusing the organization's slug --
    unlike T3's population, this script creates more than one program per
    organization, so the natural key must be per-squad, not per-franchise.

    Args:
        multi_squad: The squad's entry from
            :data:`app.services.backbone.team_program_resolution.NBA_STATS_MULTI_SQUAD_TEAM_IDS`.

    Returns:
        The natural key this script keys idempotency on (e.g.
        ``"nba-warriors-gold"``).
    """
    return f"{ORG_SLUG_PREFIX}{multi_squad.slug_suffix}"


@dataclass(frozen=True)
class PopulationTarget:
    """One sibling squad to convert into an additional ``TeamProgram`` row."""

    nba_stats_team_id: str
    org_slug: str
    team_program_slug: str
    name: str
    level: str


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
    *would* be created or skipped, so the same numbers are comparable before
    and after the real run.
    """

    planned: int = 0
    team_programs_created: int = 0
    team_programs_skipped: int = 0
    organization_missing: int = 0
    """Targets whose parent organization T3 has not created yet -- run
    ``scripts/populate_org_model_from_nba_teams.py`` first. Reported, never
    silently skipped, so an incomplete prerequisite is visible rather than
    read as "nothing to do"."""
    failures: list[PopulationFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Return the number of targets that raised and were skipped."""
        return len(self.failures)


def _load_targets() -> list[PopulationTarget]:
    """Return one population target per multi-squad id, in a stable order."""
    return [
        PopulationTarget(
            nba_stats_team_id=stats_id,
            org_slug=derive_org_slug(multi_squad.nba_team_slug),
            team_program_slug=derive_multi_squad_team_program_slug(multi_squad),
            name=multi_squad.name,
            level=multi_squad.level,
        )
        for stats_id, multi_squad in sorted(NBA_STATS_MULTI_SQUAD_TEAM_IDS.items())
    ]


def pending_from_lookup(existing_id: int | None) -> bool:
    """Return whether a row must be created, given its existing-id lookup result.

    Isolated as a pure function so the create/skip decision is unit-testable
    without a database, mirroring
    ``scripts/populate_org_model_from_nba_teams.py``'s helper of the same
    name and contract.

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


class _OrganizationNotPopulatedError(RuntimeError):
    """Raised when a sibling squad's parent organization does not exist yet.

    A distinct type (rather than a plain ``RuntimeError`` matched by message
    text) so :func:`run_population` can route this specific, expected-until-T3
    case into ``PopulationReport.organization_missing`` without pattern
    matching an error string.
    """


async def _populate_target(db: AsyncSession, target: PopulationTarget) -> bool:
    """Create the sibling ``team_program`` for one target if missing.

    Args:
        db: Active database session; the caller owns the transaction.
        target: The sibling squad to populate.

    Returns:
        ``True`` if a row was created, ``False`` if it already existed.

    Raises:
        _OrganizationNotPopulatedError: If the target's parent organization
            has not been populated yet (T3 has not run for this franchise).
            Caught by the caller and recorded as ``organization_missing``
            rather than aborting the run.
    """
    organization_id = await _find_organization_id(db, target.org_slug)
    if organization_id is None:
        raise _OrganizationNotPopulatedError(
            f"organization {target.org_slug!r} not found -- run "
            "scripts/populate_org_model_from_nba_teams.py first"
        )

    team_program_id = await _find_team_program_id(db, target.team_program_slug)
    team_program_created = pending_from_lookup(team_program_id)
    if team_program_created:
        program = TeamProgram(
            organization_id=organization_id,
            name=target.name,
            slug=target.team_program_slug,
            level=target.level,
        )
        db.add(program)
        await db.flush()

    return team_program_created


async def _plan_dry_run(
    db: AsyncSession, targets: Sequence[PopulationTarget]
) -> PopulationReport:
    """Probe every target and report pending work without writing anything."""
    report = PopulationReport(planned=len(targets))
    for target in targets:
        organization_id = await _find_organization_id(db, target.org_slug)
        if organization_id is None:
            report.organization_missing += 1
            print(
                f"DRY-RUN nba_stats_team_id={target.nba_stats_team_id} "
                f"name={target.name!r} organization={'missing'!r}"
            )
            continue

        team_program_id = await _find_team_program_id(db, target.team_program_slug)
        team_program_pending = pending_from_lookup(team_program_id)
        if team_program_pending:
            report.team_programs_created += 1
        else:
            report.team_programs_skipped += 1

        print(
            f"DRY-RUN nba_stats_team_id={target.nba_stats_team_id} "
            f"name={target.name!r} "
            f"team_program={'pending' if team_program_pending else 'exists'}"
        )
    return report


async def run_population(
    db: AsyncSession, *, dry_run: bool = False
) -> PopulationReport:
    """Run the multi-squad population and return measured counts.

    Targets are independent. A target whose parent organization is missing,
    or that otherwise raises, is recorded in ``PopulationReport.failures`` (or
    ``organization_missing``) and the run continues, so one incomplete
    franchise cannot strand the rest of the population.

    Args:
        db: Active database session; the function owns its own transactions.
        dry_run: Probe targets and report pending counts without writing.

    Returns:
        Measured counts, including per-target failures.
    """
    targets = _load_targets()
    if dry_run:
        return await _plan_dry_run(db, targets)

    report = PopulationReport(planned=len(targets))
    # A caller may have already issued reads on this session (e.g. an
    # operator probing counts, or another population step just before this
    # one), which autobegins a transaction. End it before entering the first
    # per-target transaction below -- same pattern
    # scripts/populate_org_model_from_nba_teams.py's run_population uses.
    await db.rollback()
    for target in targets:
        try:
            async with db.begin():
                team_program_created = await _populate_target(db, target)
        except _OrganizationNotPopulatedError:
            with contextlib.suppress(Exception):
                await db.rollback()
            report.organization_missing += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one bad target must not abort the run
            with contextlib.suppress(Exception):
                await db.rollback()
            report.failures.append(
                PopulationFailure(target=target, error=f"{type(exc).__name__}: {exc}")
            )
            continue

        if team_program_created:
            report.team_programs_created += 1
        else:
            report.team_programs_skipped += 1

    return report


def format_report_lines(
    report: PopulationReport, *, dry_run: bool = False
) -> list[str]:
    """Render the operator summary, including the per-target failure list."""
    label = "multi-squad team_program population" + (" (dry-run)" if dry_run else "")
    lines = [
        f"{label}: planned={report.planned} "
        f"team_programs_created={report.team_programs_created} "
        f"team_programs_skipped={report.team_programs_skipped} "
        f"organization_missing={report.organization_missing} "
        f"failed={report.failed}"
    ]
    if report.failures:
        lines.append(f"FAILED TARGETS ({report.failed}):")
        lines.extend(
            f"  nba_stats_team_id={failure.target.nba_stats_team_id} "
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
            "probe each multi-squad target for an existing team_program and "
            "report the pending counts, without writing"
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
    return 1 if (report.failures or report.organization_missing) else 0


async def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point; returns a non-zero status when any target failed."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    database_url = (
        args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    )
    return await _run(dry_run=args.dry_run, database_url=database_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

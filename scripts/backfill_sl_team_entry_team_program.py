"""Backfill ``summer_league_team_entries.team_program_id`` from ``nba_team_id``.

Ticket #784 (phase-4 journey-graph conversion). ``team_program_id`` lands
additively and nullable (see
``alembic/versions/f8855e75c831_add_team_program_id_to_sl_team_entries.py``)
beside the existing ``nba_team_id`` column -- per phase-4 spec §5.1 decision
D3, **no row is ever repointed or nulled**. This script only ever moves a team
entry from "``team_program_id`` unset" to "set to the program for its known
franchise"; an entry with a NULL ``nba_team_id`` stays NULL, because there is
nothing to derive a target from.

The join reuses the exact natural key ``scripts/populate_org_model_from_nba_teams.py``
(T3) keyed the organization/team_program population on: ``nba_team_id`` ->
``nba_teams.slug`` -> ``organizations.slug`` (``"nba-" + slug``) ->
``team_programs.slug`` (same value) -> ``team_programs.id``. So a franchise's
team entries resolve to the *same* program T3 created for it -- and the same
program ``scripts/backfill_affiliation_team_program.py`` (T4) resolves
``player_affiliations`` rows to.

Idempotent -- reruns only touch rows still missing ``team_program_id``, so a
retry (or a periodic sweep as new SL competitions ingest) creates no
duplicate work and reports zero pending once caught up.

Run (dev first; never point this at production without review):

  scripts/with-db-env.sh conda run -n draftguru python scripts/backfill_sl_team_entry_team_program.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python scripts/backfill_sl_team_entry_team_program.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402
from app.schemas.nba_teams import NbaTeam  # noqa: E402
from app.schemas.organization import Organization, TeamProgram  # noqa: E402
from app.schemas.summer_league import SummerLeagueTeamEntry  # noqa: E402
from app.utils.db_async import _prepare_asyncpg_connection  # noqa: E402
from scripts.populate_org_model_from_nba_teams import derive_org_slug  # noqa: E402


@dataclass
class BackfillReport:
    """Measured counts, comparable before/after a real run and against a dry run."""

    eligible: int = 0
    """Team entries with a non-null ``nba_team_id`` and a null ``team_program_id``."""

    updated: int = 0
    """Rows this run set ``team_program_id`` on (0 for a dry run)."""

    unresolvable: int = 0
    """Eligible rows whose ``nba_team_id`` has no matching ``team_programs`` row.

    Should be 0 once T3's population has run for every ``nba_teams`` row; a
    positive count here means T3 is incomplete, not that this script is wrong.
    """

    left_null: int = 0
    """Team entries with a null ``nba_team_id`` -- never a backfill target."""


async def _franchise_team_program_map(db: AsyncSession) -> dict[int, int]:
    """Return ``{nba_team_id: team_program_id}`` for every resolvable franchise.

    Reuses the T3 natural key (``derive_org_slug``) rather than re-deriving it,
    so this script and the population script can never silently diverge on how
    an NBA team maps to its organization/team_program pair. Identical to the
    map built in ``scripts/backfill_affiliation_team_program.py`` (T4).
    """
    query = (
        select(NbaTeam.id, NbaTeam.slug)  # type: ignore[call-overload]
        .select_from(NbaTeam)
        .order_by(NbaTeam.id)
    )
    teams = (await db.execute(query)).all()
    if not teams:
        return {}

    org_slugs = {derive_org_slug(slug): team_id for team_id, slug in teams}
    org_rows = (
        await db.execute(
            select(Organization.id, Organization.slug).where(  # type: ignore[call-overload]
                Organization.slug.in_(org_slugs)  # type: ignore[attr-defined]
            )
        )
    ).all()
    org_id_to_team_id = {org_id: org_slugs[org_slug] for org_id, org_slug in org_rows}
    if not org_id_to_team_id:
        return {}

    program_rows = (
        await db.execute(
            select(TeamProgram.organization_id, TeamProgram.id).where(  # type: ignore[call-overload]
                TeamProgram.organization_id.in_(org_id_to_team_id)  # type: ignore[attr-defined]
            )
        )
    ).all()
    return {
        org_id_to_team_id[organization_id]: program_id
        for organization_id, program_id in program_rows
        if organization_id in org_id_to_team_id
    }


async def run_backfill(db: AsyncSession, *, dry_run: bool = False) -> BackfillReport:
    """Backfill ``team_program_id`` for every eligible team entry.

    Args:
        db: Active database session; the function owns its own transaction
            when writing.
        dry_run: Compute and return counts without writing.

    Returns:
        Measured counts. In a dry run, ``updated`` is always 0 and ``eligible``
        reports what a real run would update.
    """
    franchise_map = await _franchise_team_program_map(db)

    left_null = (
        await db.scalar(
            select(func.count())
            .select_from(SummerLeagueTeamEntry)
            .where(SummerLeagueTeamEntry.nba_team_id.is_(None))  # type: ignore[union-attr]
        )
    ) or 0

    eligible_query = (
        select(func.count())
        .select_from(SummerLeagueTeamEntry)
        .where(
            SummerLeagueTeamEntry.nba_team_id.isnot(None),  # type: ignore[union-attr]
            SummerLeagueTeamEntry.team_program_id.is_(None),  # type: ignore[union-attr]
        )
    )
    eligible = (await db.scalar(eligible_query)) or 0

    if not franchise_map:
        # T3 hasn't populated any organizations/team_programs yet; every
        # eligible row is unresolvable until that population runs.
        return BackfillReport(
            eligible=eligible,
            unresolvable=eligible,
            left_null=left_null,
        )

    unresolvable_query = eligible_query.where(
        SummerLeagueTeamEntry.nba_team_id.notin_(franchise_map)  # type: ignore[union-attr]
    )
    unresolvable = (await db.scalar(unresolvable_query)) or 0

    if dry_run:
        return BackfillReport(
            eligible=eligible,
            unresolvable=unresolvable,
            left_null=left_null,
        )

    updated = 0
    # The counting queries above autobegin a read transaction on this
    # AsyncSession; end it before entering the write transaction below (same
    # pattern as scripts/backfill_affiliation_team_program.py's run_backfill).
    await db.rollback()
    async with db.begin():
        for nba_team_id, team_program_id in franchise_map.items():
            result = await db.execute(
                update(SummerLeagueTeamEntry)
                .where(
                    SummerLeagueTeamEntry.nba_team_id == nba_team_id,  # type: ignore[arg-type]
                    SummerLeagueTeamEntry.team_program_id.is_(None),  # type: ignore[union-attr]
                )
                .values(team_program_id=team_program_id)
            )
            updated += result.rowcount or 0

    return BackfillReport(
        eligible=eligible,
        updated=updated,
        unresolvable=unresolvable,
        left_null=left_null,
    )


def format_report_lines(report: BackfillReport, *, dry_run: bool = False) -> list[str]:
    """Render the operator summary for dry-run and ticket evidence."""
    label = "summer_league_team_entries team_program_id backfill" + (
        " (dry-run)" if dry_run else ""
    )
    return [
        f"{label}: eligible={report.eligible} updated={report.updated} "
        f"unresolvable={report.unresolvable} left_null={report.left_null}"
    ]


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report pending/unresolvable counts without writing",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Defaults to DATABASE_URL / the app's configured database.",
    )
    return parser


async def _run(*, dry_run: bool, database_url: str) -> int:
    """Open a session against ``database_url`` and run the backfill."""
    normalized_url, connect_args = _prepare_asyncpg_connection(database_url)
    engine = create_async_engine(normalized_url, echo=False, connect_args=connect_args)
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with session_factory() as db:
            report = await run_backfill(db, dry_run=dry_run)
    finally:
        await engine.dispose()

    for line in format_report_lines(report, dry_run=dry_run):
        print(line)
    return 1 if report.unresolvable else 0


async def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns non-zero when any eligible row is unresolvable."""
    args = build_parser().parse_args(argv)
    database_url = (
        args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    )
    return await _run(dry_run=args.dry_run, database_url=database_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

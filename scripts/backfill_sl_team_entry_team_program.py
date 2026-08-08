"""Backfill ``summer_league_team_entries.team_program_id`` from two strategies.

Ticket #784 (phase-4 journey-graph conversion) shipped strategy 1 below.
``team_program_id`` lands additively and nullable (see
``alembic/versions/f8855e75c831_add_team_program_id_to_sl_team_entries.py``)
beside the existing ``nba_team_id`` column -- per phase-4 spec §5.1 decision
D3, **no row is ever repointed or nulled**. Both strategies only ever move a
team entry from "``team_program_id`` unset" to "set to the program for its
known franchise".

Strategy 1 -- franchise join (``nba_team_id`` -> franchise -> program). Reuses
``scripts/_franchise_team_program_map.py``'s shared bridge, which delegates to
the backbone resolver (``app.services.backbone.team_program_resolution``,
#796): ``nba_team_id`` -> ``nba_teams.slug`` -> ``organizations.slug``
(``"nba-" + slug``) -> the organization's ``team_programs`` row(s) ->
``team_programs.id``, raising ``AmbiguousTeamProgramError`` rather than
guessing if an organization owns more than one program. A team entry with a
NULL ``nba_team_id`` has nothing for this join to key on and is left alone.

Strategy 2 -- provider id resolution (#808). #784's backfill (and #796's
ingest-time write) only ever populated ``nba_team_id``; no code in this repo
has ever *written* it for historical rows. Measuring dev after #807 found 103
of 622 team entries with ``nba_team_id`` NULL, and 82 of those carry a real
NBA franchise ``nba_stats_team_id`` (e.g. Atlanta Hawks ``1610612737``) that
was simply never backfilled -- only ~21 are genuinely non-NBA squads (Team
China, Croatia, D-League Select) that correctly stay NULL forever. This
strategy resolves those 82 via
``app.services.backbone.team_program_resolution.resolve_team_targets`` -- the
exact function #796 already calls at ingest time -- which maps
``nba_stats_team_id`` -> ``NBA_STATS_TEAM_ID_TO_ABBREVIATION`` -> ``nba_teams``
-> the same ``"nba-" + slug`` organization key -> ``team_programs``. Every id
present in ``summer_league_team_entries`` that neither that map nor
``NBA_STATS_MULTI_SQUAD_TEAM_IDS`` (#810, the second/third Summer League
squad ids) covers is reported by name, not silently skipped -- a silent skip
here is exactly how the original 103 went unnoticed in the first place
(#784 assumed all 103 were non-NBA without measuring), and it is exactly how
#810's 8 multi-squad ids were *found*.

Both strategies are strictly additive and idempotent -- reruns only touch
rows still missing both ``nba_team_id`` and ``team_program_id``. Their
eligibility is mutually exclusive by construction (strategy 1 requires
``nba_team_id IS NOT NULL``; strategy 2 requires it ``IS NULL``), so the two
can never disagree about, or repoint, the same row -- unlike
``scripts/backfill_affiliation_team_program.py``'s two strategies, which can
both reach the same affiliation and so need an explicit disagreement counter.
Every write here still re-checks ``nba_team_id IS NULL AND team_program_id IS
NULL`` in its ``WHERE`` clause so a concurrent write between the read and the
write can never repoint a row.

Re-run ``scripts/backfill_affiliation_team_program.py`` after this script --
its participation bridge (#807) reaches every affiliation hanging off a team
entry this strategy newly resolves.

Run (dev first; never point this at production without review):

  scripts/with-db-env.sh conda run -n draftguru python scripts/backfill_sl_team_entry_team_program.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python scripts/backfill_sl_team_entry_team_program.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402
from app.schemas.summer_league import SummerLeagueTeamEntry  # noqa: E402
from app.services.backbone.team_program_resolution import (  # noqa: E402
    NBA_STATS_MULTI_SQUAD_TEAM_IDS,
    NBA_STATS_TEAM_ID_TO_ABBREVIATION,
    resolve_team_targets,
)
from app.utils.db_async import _prepare_asyncpg_connection  # noqa: E402
from scripts._franchise_team_program_map import (  # noqa: E402
    franchise_nba_team_id_to_team_program_id,
)


@dataclass
class StatsIdReport:
    """Counts for the second (``nba_stats_team_id``) resolution strategy."""

    eligible: int = 0
    """Team entries with ``nba_team_id`` NULL, ``team_program_id`` NULL, and
    an ``nba_stats_team_id`` covered by ``NBA_STATS_TEAM_ID_TO_ABBREVIATION``
    -- candidates this strategy can attempt to resolve."""

    updated: int = 0
    """Rows this run set both ``nba_team_id`` and ``team_program_id`` on via
    this strategy (0 for a dry run)."""

    unresolvable: int = 0
    """Eligible (covered) rows the backbone resolver still could not fully
    resolve -- e.g. ``nba_teams`` unseeded for that abbreviation, T3's org
    model not yet populated for the franchise, or an ambiguous organization.
    Should be 0 once T3/seeding are complete; a positive count here means
    those are incomplete, not that this script is wrong."""

    uncovered: int = 0
    """Rows whose ``nba_stats_team_id`` is not in
    ``NBA_STATS_TEAM_ID_TO_ABBREVIATION`` -- reported rather than silently
    skipped. Includes genuinely non-NBA squads (Team China, Croatia,
    D-League Select), which correctly stay NULL forever, alongside any
    future coverage gap the map needs to grow to close."""

    uncovered_stats_ids: list[str] = field(default_factory=list)
    """The distinct uncovered ids themselves (sorted), for operator
    visibility -- exactly what #784 assuming-without-measuring cost this
    repo once already."""


@dataclass
class BackfillReport:
    """Measured counts, comparable before/after a real run and against a dry run."""

    eligible: int = 0
    """Team entries with a non-null ``nba_team_id`` and a null ``team_program_id``."""

    updated: int = 0
    """Rows strategy 1 (franchise join) set ``team_program_id`` on (0 for a dry run)."""

    unresolvable: int = 0
    """Eligible rows whose ``nba_team_id`` has no matching ``team_programs`` row.

    Should be 0 once T3's population has run for every ``nba_teams`` row; a
    positive count here means T3 is incomplete, not that this script is wrong.
    """

    left_null: int = 0
    """Team entries with a null ``nba_team_id`` -- never a strategy-1 target.

    Some of these may still be resolved by strategy 2 (reported separately
    on ``stats_id``, see :class:`StatsIdReport`) -- this counter describes
    strategy 1's reach only.
    """

    stats_id: StatsIdReport = field(default_factory=StatsIdReport)
    """Strategy 2 (``nba_stats_team_id`` resolution, #808) counts, reported
    separately so it is visible which path resolved what."""


async def _stats_id_targets(
    db: AsyncSession,
) -> tuple[dict[str, tuple[int, int]], int, int, list[str]]:
    """Resolve every still-unset, covered ``nba_stats_team_id`` to its dual target.

    Args:
        db: Active database session (read-only; issues no writes).

    Returns:
        ``(resolved, eligible, unresolvable, uncovered_ids)``. ``resolved``
        maps ``nba_stats_team_id`` -> ``(nba_team_id, team_program_id)`` for
        ids the backbone resolver fully resolved. ``eligible`` counts rows
        (not distinct ids) whose id is covered by
        ``NBA_STATS_TEAM_ID_TO_ABBREVIATION`` or ``NBA_STATS_MULTI_SQUAD_TEAM_IDS``
        (#810). ``unresolvable`` counts the subset of those rows whose id is
        covered but the resolver still could not produce a
        ``team_program_id`` (nba_teams unseeded, T3/multi-squad population
        not yet run for the franchise, or an ambiguous organization).
        ``uncovered_ids`` lists the distinct ids (sorted) neither map covers
        at all -- reported, never silently skipped, per #808. This is exactly
        the report that surfaced #810's 8 multi-squad ids in the first place.
    """
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueTeamEntry.nba_stats_team_id, func.count()
            )
            .select_from(SummerLeagueTeamEntry)
            .where(
                SummerLeagueTeamEntry.nba_team_id.is_(None),  # type: ignore[union-attr]
                SummerLeagueTeamEntry.team_program_id.is_(None),  # type: ignore[union-attr]
            )
            .group_by(SummerLeagueTeamEntry.nba_stats_team_id)
        )
    ).all()

    resolved: dict[str, tuple[int, int]] = {}
    eligible = 0
    unresolvable = 0
    uncovered_ids: list[str] = []

    for stats_id, row_count in rows:
        if (
            stats_id not in NBA_STATS_TEAM_ID_TO_ABBREVIATION
            and stats_id not in NBA_STATS_MULTI_SQUAD_TEAM_IDS
        ):
            uncovered_ids.append(stats_id)
            continue
        eligible += row_count
        nba_team_id, team_program_id = await resolve_team_targets(
            db, nba_stats_team_id=stats_id
        )
        if nba_team_id is not None and team_program_id is not None:
            resolved[stats_id] = (nba_team_id, team_program_id)
        else:
            unresolvable += row_count

    return resolved, eligible, unresolvable, sorted(uncovered_ids)


async def _apply_stats_id_updates(
    db: AsyncSession, resolved: dict[str, tuple[int, int]]
) -> int:
    """Write ``resolved`` (``nba_stats_team_id`` -> ``(nba_team_id, team_program_id)``).

    Re-checks ``nba_team_id IS NULL AND team_program_id IS NULL`` in the
    ``WHERE`` clause so a concurrent write between the read above and this
    write can never repoint a row -- same guard strategy 1 uses.
    """
    if not resolved:
        return 0

    updated = 0
    for stats_id, (nba_team_id, team_program_id) in resolved.items():
        result = await db.execute(
            update(SummerLeagueTeamEntry)
            .where(
                SummerLeagueTeamEntry.nba_stats_team_id == stats_id,  # type: ignore[arg-type]
                SummerLeagueTeamEntry.nba_team_id.is_(None),  # type: ignore[union-attr]
                SummerLeagueTeamEntry.team_program_id.is_(None),  # type: ignore[union-attr]
            )
            .values(nba_team_id=nba_team_id, team_program_id=team_program_id)
        )
        updated += result.rowcount or 0
    return updated


async def run_backfill(db: AsyncSession, *, dry_run: bool = False) -> BackfillReport:
    """Backfill ``team_program_id`` for every eligible team entry, both strategies.

    Args:
        db: Active database session; the function owns its own transaction
            when writing.
        dry_run: Compute and return counts without writing.

    Returns:
        Measured counts. In a dry run, ``updated``/``stats_id.updated`` are
        always 0 and ``eligible``/``stats_id.eligible`` report what a real
        run would update.
    """
    # --- Strategy 1: franchise join (nba_team_id -> team_program_id) ---
    franchise_map = await franchise_nba_team_id_to_team_program_id(db)

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

    if franchise_map:
        unresolvable_query = eligible_query.where(
            SummerLeagueTeamEntry.nba_team_id.notin_(franchise_map)  # type: ignore[union-attr]
        )
        unresolvable = (await db.scalar(unresolvable_query)) or 0
    else:
        # T3 hasn't populated any organizations/team_programs yet; every
        # eligible row is unresolvable until that population runs.
        unresolvable = eligible

    updated = 0
    if not dry_run and franchise_map:
        # The counting queries above autobegin a read transaction on this
        # AsyncSession; end it before entering the write transaction below
        # (same pattern as scripts/backfill_affiliation_team_program.py's
        # run_backfill).
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

    # --- Strategy 2: provider id resolution (#808) ---
    # Independent of strategy 1 -- runs regardless of whether the franchise
    # map resolved anything, since it targets rows with nba_team_id NULL,
    # which strategy 1 never touches.
    (
        resolved,
        stats_eligible,
        stats_unresolvable,
        uncovered_ids,
    ) = await _stats_id_targets(db)

    stats_updated = 0
    if not dry_run and resolved:
        await db.rollback()
        async with db.begin():
            stats_updated = await _apply_stats_id_updates(db, resolved)

    stats_id_report = StatsIdReport(
        eligible=stats_eligible,
        updated=stats_updated,
        unresolvable=stats_unresolvable,
        uncovered=len(uncovered_ids),
        uncovered_stats_ids=uncovered_ids,
    )

    return BackfillReport(
        eligible=eligible,
        updated=updated,
        unresolvable=unresolvable,
        left_null=left_null,
        stats_id=stats_id_report,
    )


def format_report_lines(report: BackfillReport, *, dry_run: bool = False) -> list[str]:
    """Render the operator summary for dry-run and ticket evidence.

    The strategy-1 line's fields/format never change shape -- #799 asserted
    this exact string for the single-strategy case and that assertion must
    keep passing. A second line for strategy 2 (#808, ``nba_stats_team_id``
    resolution) only appears when it has something to report, so a run
    where that strategy found nothing produces the identical single-line
    output prior tickets depend on.
    """
    label = "summer_league_team_entries team_program_id backfill" + (
        " (dry-run)" if dry_run else ""
    )
    lines = [
        f"{label}: eligible={report.eligible} updated={report.updated} "
        f"unresolvable={report.unresolvable} left_null={report.left_null}"
    ]
    s = report.stats_id
    if s.eligible or s.updated or s.unresolvable or s.uncovered:
        ids_note = (
            f" uncovered_ids={','.join(s.uncovered_stats_ids)}"
            if s.uncovered_stats_ids
            else ""
        )
        lines.append(
            f"{label} (nba_stats_team_id): eligible={s.eligible} "
            f"updated={s.updated} unresolvable={s.unresolvable} "
            f"uncovered={s.uncovered}{ids_note}"
        )
    return lines


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
    return 1 if (report.unresolvable or report.stats_id.unresolvable) else 0


async def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns non-zero when any eligible row is unresolvable under either
    strategy.
    """
    args = build_parser().parse_args(argv)
    database_url = (
        args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    )
    return await _run(dry_run=args.dry_run, database_url=database_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Backfill ``player_affiliations.team_program_id`` from two strategies.

Ticket #783 (phase-4 journey-graph conversion, T4). ``team_program_id`` lands
additively and nullable (see ``alembic/versions/c2d3e4f5a6b7_...``) beside the
existing ``nba_team_id`` column -- per phase-4 spec §5.1 decision D3, **no row
is ever repointed or nulled**. This script only ever moves an affiliation from
"``team_program_id`` unset" to "set to the program for its known franchise".

Two independent resolution strategies run, in order:

Strategy 1 -- franchise join (``nba_team_id`` -> franchise -> program). Reuses
``scripts/_franchise_team_program_map.py``'s shared bridge, which delegates to
the backbone resolver (``app.services.backbone.team_program_resolution``,
#796): ``nba_team_id`` -> ``nba_teams.slug`` -> ``organizations.slug``
(``"nba-" + slug``) -> the organization's ``team_programs`` row(s) ->
``team_programs.id``, raising ``AmbiguousTeamProgramError`` rather than
guessing if an organization owns more than one program. An affiliation with a
NULL ``nba_team_id`` has nothing for this join to key on and is left alone.

Strategy 2 -- participation bridge (#807). Every historical affiliation this
ticket exists for has ``nba_team_id`` NULL *too* -- strategy 1 can never reach
it. The only path in is ``summer_league_participation.affiliation_id ->
summer_league_team_entries.{nba_team_id, team_program_id}``: the team entry
carries a resolved target (from #784's backfill or #796's ingest-time write)
even though the affiliation itself predates that resolution. This walks that
bridge to find each participation's *latest* affiliation and the program its
team entry resolved to.

Supersession decision (D3 corollary, decided here): ``player_affiliations`` is
append-only, and a supersede chain holds several rows for one participation --
``participation.affiliation_id`` points only at the *latest* one. This
strategy walks ``supersedes_id`` back up the whole chain and backfills every
row in it, not just the latest. A superseded row is a historical assertion in
its own right; leaving it NULL while its successor carries a target would
make the chain internally inconsistent for a reader comparing assertions
across it -- the target would appear to spring from nowhere partway through
the chain. The alternative (stamp only the latest row) was rejected for that
reason.

Both strategies are strictly additive and idempotent -- reruns only touch
rows still missing ``team_program_id``. An affiliation that already carries a
``team_program_id`` (from either strategy, or from ingest-time writes) is
never repointed, even when the participation bridge would resolve it to a
*different* program -- that disagreement is counted
(``bridge.already_set_disagreement``), not applied, since a nonzero count
means the bridge and an already-set value disagree. Two participation rows
disagreeing with each other about one affiliation's target is refused the
same way (``bridge.ambiguous_participation``), per this repo's
entity-resolution rule: ambiguous or unknown resolves to NULL, never a guess.

Run (dev first; never point this at production without review):

  scripts/with-db-env.sh conda run -n draftguru python scripts/backfill_affiliation_team_program.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python scripts/backfill_affiliation_team_program.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402
from app.schemas.player_affiliation import PlayerAffiliation  # noqa: E402
from app.schemas.summer_league import (  # noqa: E402
    SummerLeagueParticipation,
    SummerLeagueTeamEntry,
)
from app.utils.db_async import _prepare_asyncpg_connection  # noqa: E402
from scripts._franchise_team_program_map import (  # noqa: E402
    franchise_nba_team_id_to_team_program_id,
)


@dataclass
class BridgeReport:
    """Counts for the second (participation-bridge) resolution strategy."""

    eligible: int = 0
    """NULL-``team_program_id`` rows (across every supersede chain reached) the
    participation bridge can resolve a target for."""

    updated: int = 0
    """Rows this run set ``team_program_id`` on via the bridge (0 for a dry run)."""

    already_set_disagreement: int = 0
    """Rows already carrying a ``team_program_id`` that differs from what the
    bridge would resolve. Never repointed -- only counted. A nonzero count
    means the bridge and an already-set value (e.g. strategy 1's franchise
    join) disagree, which is worth an operator's attention."""

    ambiguous_participation: int = 0
    """Affiliations reached by two or more participation rows whose team
    entries resolve to different programs. Refused rather than guessed, per
    this repo's entity-resolution rule."""


@dataclass
class BackfillReport:
    """Measured counts, comparable before/after a real run and against a dry run."""

    eligible: int = 0
    """Affiliations with a non-null ``nba_team_id`` and a null ``team_program_id``."""

    updated: int = 0
    """Rows strategy 1 (franchise join) set ``team_program_id`` on (0 for a dry run)."""

    unresolvable: int = 0
    """Eligible rows whose ``nba_team_id`` has no matching ``team_programs`` row.

    Should be 0 once T3's population has run for every ``nba_teams`` row; a
    positive count here means T3 is incomplete, not that this script is wrong.
    """

    left_null: int = 0
    """Affiliations with a null ``nba_team_id`` -- never a strategy-1 target.

    Some of these may still be resolved by strategy 2 (the participation
    bridge, reported separately on ``bridge``) -- this counter describes
    strategy 1's reach only.
    """

    bridge: BridgeReport = field(default_factory=BridgeReport)
    """Strategy 2 (participation-bridge) counts, reported separately so it is
    visible which path resolved what."""


async def _participation_bridge_targets(
    db: AsyncSession,
) -> tuple[dict[int, int], int]:
    """Map each participation's *latest* affiliation id to its team entry's program.

    Args:
        db: Active database session (read-only; issues no writes).

    Returns:
        ``(targets, ambiguous_count)``. ``targets`` maps
        ``player_affiliations.id`` (the *latest* row for a participation, per
        ``participation.affiliation_id``) to the ``team_program_id`` its team
        entry carries. ``ambiguous_count`` is the number of affiliation ids
        reached by more than one participation row whose team entries
        disagree on the target -- excluded from ``targets`` and refused
        rather than guessed.
    """
    rows = (
        await db.execute(
            select(
                SummerLeagueParticipation.affiliation_id,  # type: ignore[call-overload]
                SummerLeagueTeamEntry.team_program_id,
            )
            .select_from(SummerLeagueParticipation)
            .join(
                SummerLeagueTeamEntry,
                SummerLeagueTeamEntry.id == SummerLeagueParticipation.team_entry_id,  # type: ignore[arg-type]
            )
            .where(
                SummerLeagueParticipation.affiliation_id.isnot(None),  # type: ignore[union-attr]
                SummerLeagueTeamEntry.team_program_id.isnot(None),  # type: ignore[union-attr]
            )
        )
    ).all()

    candidates: dict[int, set[int]] = defaultdict(set)
    for affiliation_id, team_program_id in rows:
        candidates[affiliation_id].add(team_program_id)

    targets: dict[int, int] = {}
    ambiguous = 0
    for affiliation_id, program_ids in candidates.items():
        if len(program_ids) == 1:
            targets[affiliation_id] = next(iter(program_ids))
        else:
            ambiguous += 1
    return targets, ambiguous


async def _expand_supersede_chains(
    db: AsyncSession, latest_targets: dict[int, int]
) -> tuple[dict[int, int], int]:
    """Propagate each latest row's resolved target down its full supersede chain.

    See the module docstring's "Supersession decision" for why the whole
    chain, not just the latest row, is backfilled.

    Args:
        db: Active database session (read-only; issues no writes).
        latest_targets: ``{affiliation_id: team_program_id}`` for each
            participation's latest row, from :func:`_participation_bridge_targets`.

    Returns:
        ``(updates, already_set_disagreement)``. ``updates`` maps every
        still-NULL affiliation id across every chain (latest row plus every
        ancestor reachable via ``supersedes_id``) to the target its chain
        resolved to. ``already_set_disagreement`` counts rows in those chains
        that already carry a *different* ``team_program_id`` -- never
        repointed, only counted.
    """
    if not latest_targets:
        return {}, 0

    # Load every affiliation's (team_program_id, supersedes_id) once so the
    # chain walk below is pure in-memory traversal, not one round-trip per
    # ancestor. Affiliation volume is small (a handful of thousand rows at
    # most for this table), so loading the whole column pair is cheap and
    # far simpler than a recursive CTE for a one-time operator script.
    all_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                PlayerAffiliation.id,
                PlayerAffiliation.team_program_id,
                PlayerAffiliation.supersedes_id,
            )
        )
    ).all()
    by_id: dict[int, tuple[int | None, int | None]] = {
        row[0]: (row[1], row[2]) for row in all_rows
    }

    updates: dict[int, int] = {}
    disagreements = 0
    for root_id, target in latest_targets.items():
        current_id: int | None = root_id
        seen: set[int] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            row = by_id.get(current_id)
            if row is None:
                break
            existing_team_program_id, supersedes_id = row
            if existing_team_program_id is None:
                updates[current_id] = target
            elif existing_team_program_id != target:
                disagreements += 1
            current_id = supersedes_id

    return updates, disagreements


async def _apply_bridge_updates(db: AsyncSession, updates: dict[int, int]) -> int:
    """Write ``updates`` (affiliation id -> team_program_id), grouped by target value.

    Re-checks ``team_program_id IS NULL`` in the ``WHERE`` clause so a
    concurrent write between the read above and this write can never
    repoint a row -- same guard strategy 1 uses.
    """
    if not updates:
        return 0

    grouped: dict[int, list[int]] = defaultdict(list)
    for affiliation_id, team_program_id in updates.items():
        grouped[team_program_id].append(affiliation_id)

    updated = 0
    for team_program_id, affiliation_ids in grouped.items():
        result = await db.execute(
            update(PlayerAffiliation)
            .where(
                PlayerAffiliation.id.in_(affiliation_ids),  # type: ignore[union-attr]
                PlayerAffiliation.team_program_id.is_(None),  # type: ignore[union-attr]
            )
            .values(team_program_id=team_program_id)
        )
        updated += result.rowcount or 0
    return updated


async def run_backfill(db: AsyncSession, *, dry_run: bool = False) -> BackfillReport:
    """Backfill ``team_program_id`` for every eligible affiliation, both strategies.

    Args:
        db: Active database session; the function owns its own transaction
            when writing.
        dry_run: Compute and return counts without writing.

    Returns:
        Measured counts. In a dry run, ``updated``/``bridge.updated`` are
        always 0 and ``eligible``/``bridge.eligible`` report what a real run
        would update.
    """
    # --- Strategy 1: franchise join (nba_team_id -> team_program_id) ---
    franchise_map = await franchise_nba_team_id_to_team_program_id(db)

    left_null = (
        await db.scalar(
            select(func.count())
            .select_from(PlayerAffiliation)
            .where(PlayerAffiliation.nba_team_id.is_(None))  # type: ignore[union-attr]
        )
    ) or 0

    eligible_query = (
        select(func.count())
        .select_from(PlayerAffiliation)
        .where(
            PlayerAffiliation.nba_team_id.isnot(None),  # type: ignore[union-attr]
            PlayerAffiliation.team_program_id.is_(None),  # type: ignore[union-attr]
        )
    )
    eligible = (await db.scalar(eligible_query)) or 0

    if franchise_map:
        unresolvable_query = eligible_query.where(
            PlayerAffiliation.nba_team_id.notin_(franchise_map)  # type: ignore[union-attr]
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
        # (same pattern as populate_org_model_from_nba_teams.run_population).
        await db.rollback()
        async with db.begin():
            for nba_team_id, team_program_id in franchise_map.items():
                result = await db.execute(
                    update(PlayerAffiliation)
                    .where(
                        PlayerAffiliation.nba_team_id == nba_team_id,  # type: ignore[arg-type]
                        PlayerAffiliation.team_program_id.is_(None),  # type: ignore[union-attr]
                    )
                    .values(team_program_id=team_program_id)
                )
                updated += result.rowcount or 0

    # --- Strategy 2: participation bridge (#807) ---
    # Independent of strategy 1 -- runs regardless of whether the franchise
    # map resolved anything, since it derives its target directly from
    # summer_league_team_entries.team_program_id, not from nba_team_id.
    latest_targets, ambiguous_participation = await _participation_bridge_targets(db)
    bridge_updates, already_set_disagreement = await _expand_supersede_chains(
        db, latest_targets
    )

    bridge_updated = 0
    if not dry_run and bridge_updates:
        await db.rollback()
        async with db.begin():
            bridge_updated = await _apply_bridge_updates(db, bridge_updates)

    bridge = BridgeReport(
        eligible=len(bridge_updates),
        updated=bridge_updated,
        already_set_disagreement=already_set_disagreement,
        ambiguous_participation=ambiguous_participation,
    )

    return BackfillReport(
        eligible=eligible,
        updated=updated,
        unresolvable=unresolvable,
        left_null=left_null,
        bridge=bridge,
    )


def format_report_lines(report: BackfillReport, *, dry_run: bool = False) -> list[str]:
    """Render the operator summary for dry-run and ticket evidence.

    The strategy-1 line's fields/format never change shape -- #799 asserted
    this exact string for the single-strategy case and that assertion must
    keep passing. A second line for strategy 2 (the participation bridge)
    only appears when it has something to report, so a run where the bridge
    found nothing (e.g. no Summer League participation rows exist yet)
    produces the identical single-line output prior tickets depend on.
    """
    label = "player_affiliations team_program_id backfill" + (
        " (dry-run)" if dry_run else ""
    )
    lines = [
        f"{label}: eligible={report.eligible} updated={report.updated} "
        f"unresolvable={report.unresolvable} left_null={report.left_null}"
    ]
    b = report.bridge
    if (
        b.eligible
        or b.updated
        or b.already_set_disagreement
        or b.ambiguous_participation
    ):
        lines.append(
            f"{label} (participation bridge): eligible={b.eligible} "
            f"updated={b.updated} already_set_disagreement={b.already_set_disagreement} "
            f"ambiguous_participation={b.ambiguous_participation}"
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

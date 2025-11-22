from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast

from sqlalchemy import select, update, delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.fields import CohortType, MetricSource
from app.schemas.metrics import (
    MetricDefinition,
    MetricSnapshot,
    PlayerMetricValue,
    PlayerSimilarity,
)
from app.utils.db_async import SessionLocal, load_schema_modules


@dataclass
class WorkItem:
    snapshot_id: int
    run_key: str
    cohort: CohortType
    season_id: Optional[int]
    pos_parent: Optional[str]
    pos_fine: Optional[str]
    notes: Optional[str]
    calculated_at: Optional[object]


async def find_advanced_snapshots(
    session: AsyncSession, cohorts: Optional[Set[CohortType]]
) -> List[WorkItem]:
    stmt = select(
        MetricSnapshot.id,
        MetricSnapshot.run_key,
        MetricSnapshot.cohort,
        MetricSnapshot.season_id,
        MetricSnapshot.position_scope_parent,
        MetricSnapshot.position_scope_fine,
        MetricSnapshot.notes,
        MetricSnapshot.calculated_at,
    ).where(MetricSnapshot.source == MetricSource.advanced_stats)
    if cohorts:
        stmt = stmt.where(cast(Any, MetricSnapshot.cohort).in_(list(cohorts)))
    result = await session.execute(stmt)
    items: List[WorkItem] = []
    for row in result.all():
        items.append(
            WorkItem(
                snapshot_id=row[0],
                run_key=row[1],
                cohort=row[2],
                season_id=row[3],
                pos_parent=row[4],
                pos_fine=row[5],
                notes=row[6],
                calculated_at=row[7],
            )
        )
    return items


async def distinct_sources_for_snapshot(
    session: AsyncSession, snapshot_id: int
) -> List[MetricSource]:
    res = await session.execute(
        select(MetricDefinition.source)
        .join(
            PlayerMetricValue,
            PlayerMetricValue.metric_definition_id == MetricDefinition.id,
        )
        .where(PlayerMetricValue.snapshot_id == snapshot_id)
        .distinct()
    )
    return [row[0] for row in res.all()]


async def ids_for_source(
    session: AsyncSession, snapshot_id: int, source: MetricSource
) -> Tuple[List[int], Set[int]]:
    # Return (pmv_ids, player_ids)
    res = await session.execute(
        select(PlayerMetricValue.id, PlayerMetricValue.player_id)
        .join(
            MetricDefinition,
            PlayerMetricValue.metric_definition_id == MetricDefinition.id,
        )
        .where(
            PlayerMetricValue.snapshot_id == snapshot_id,
            MetricDefinition.source == source,
        )
    )
    pmv_ids: List[int] = []
    player_ids: Set[int] = set()
    for pmv_id, player_id in res.all():
        pmv_ids.append(pmv_id)
        player_ids.add(player_id)
    return pmv_ids, player_ids


async def split_snapshot(
    session: AsyncSession,
    item: WorkItem,
    *,
    execute: bool,
    force_delete_similarity: bool,
) -> None:
    # Inspect sources present
    sources = await distinct_sources_for_snapshot(session, item.snapshot_id)
    src_vals = {getattr(s, "value", s) for s in sources}
    if not sources:
        print(
            f"[skip] Snapshot {item.snapshot_id} has no PlayerMetricValue rows; deleting mislabeled snapshot."
        )
        if execute:
            await session.execute(
                delete(MetricSnapshot).where(MetricSnapshot.id == item.snapshot_id)
            )
            await session.commit()
        return

    # Check if any PlayerSimilarity rows exist and handle policy
    sim_res = await session.execute(
        select(PlayerSimilarity.id).where(
            PlayerSimilarity.snapshot_id == item.snapshot_id
        )
    )
    sim_ids = [row[0] for row in sim_res.all()]
    if sim_ids and not force_delete_similarity:
        print(
            f"[abort] Snapshot {item.snapshot_id} has {len(sim_ids)} PlayerSimilarity rows; rerun with --force-delete-sim to delete them."
        )
        return

    print(
        f"[plan] Split snapshot {item.snapshot_id} run_key={item.run_key} into sources={sorted(src_vals)}"
    )
    if not execute:
        return

    # Optionally delete similarity rows first
    if sim_ids:
        await session.execute(
            delete(PlayerSimilarity).where(cast(Any, PlayerSimilarity.id).in_(sim_ids))
        )

    # Create a new snapshot per source and reassign pmv rows
    created: Dict[MetricSource, int] = {}
    for src in sources:
        pmv_ids, players = await ids_for_source(session, item.snapshot_id, src)
        if not pmv_ids:
            continue

        new_run_key = f"{item.run_key}:{getattr(src, 'value', src)}"
        snap = MetricSnapshot(
            run_key=new_run_key,
            cohort=item.cohort,
            season_id=item.season_id,
            position_scope_parent=item.pos_parent,
            position_scope_fine=item.pos_fine,
            source=src,
            population_size=len(players),
            notes=item.notes,
            calculated_at=item.calculated_at,
        )
        session.add(snap)
        await session.flush()
        created[src] = snap.id  # type: ignore[assignment]

        # Reassign the pmv rows
        await session.execute(
            update(PlayerMetricValue)
            .where(cast(Any, PlayerMetricValue.id).in_(pmv_ids))
            .values(snapshot_id=snap.id)
        )

    # Delete the original mislabeled snapshot
    await session.execute(
        delete(MetricSnapshot).where(MetricSnapshot.id == item.snapshot_id)
    )
    await session.commit()
    print(
        f"[done] Snapshot {item.snapshot_id} split into {[getattr(s,'value',s) for s in created.keys()]}"
    )


async def main_async(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Split mislabeled 'advanced_stats' metric snapshots into separate combine_* sources"
        )
    )
    parser.add_argument(
        "--cohorts",
        nargs="+",
        choices=[c.value for c in CohortType],
        help="Limit to these cohorts (default: all cohorts)",
    )
    parser.add_argument(
        "--run-keys",
        nargs="+",
        help="Optionally limit to specific run_keys",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply changes; otherwise runs in dry-run mode",
    )
    parser.add_argument(
        "--force-delete-sim",
        action="store_true",
        help=(
            "If the snapshot has PlayerSimilarity rows, delete them to allow splitting."
        ),
    )
    args = parser.parse_args(argv)

    load_schema_modules()
    async with SessionLocal() as session:
        cohort_set = {CohortType(c) for c in args.cohorts} if args.cohorts else None
        items = await find_advanced_snapshots(session, cohort_set)
        if args.run_keys:
            rk = set(args.run_keys)
            items = [i for i in items if i.run_key in rk]
        if not items:
            print("No mislabeled advanced_stats snapshots found matching filters.")
            return
        for item in items:
            await split_snapshot(
                session,
                item,
                execute=args.execute,
                force_delete_similarity=args.force_delete_sim,
            )


def main(argv: Optional[Sequence[str]] = None) -> None:
    asyncio.run(main_async(argv))


if __name__ == "__main__":
    main()

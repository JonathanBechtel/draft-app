r"""Backfill 5 synthetic consensus snapshots with backdated timestamps.

This is **demo data**, run once against a dev / staging database so the
homepage sparklines and delta values have something to render before real
historical board ingestion catches up. Each synthetic snapshot perturbs
the prior snapshot's ranks via a random walk; the closer in time to the
current real snapshot, the smaller the perturbation. The current real
snapshot's ``prev_rank`` / ``rank_delta`` are then updated to be vs the
most-recent synthetic, so the latest snapshot shows real movement.

Usage::

    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/seed_synthetic_consensus_history.py

Idempotent + safe: every snapshot this script creates is sentinel-tagged
with ``num_boards=0`` and ``board_ids=[]``, which a real
``recompute_consensus`` call would never produce (a real recompute
requires at least one approved board). Re-running this script only
removes rows that carry that sentinel, so legitimate historical
recomputes — even ones using ``trigger=MANUAL`` — are preserved.

Reversibility::

    DELETE FROM consensus_snapshots
    WHERE draft_year=2026 AND id <> 1
      AND trigger='MANUAL' AND num_boards=0;
    -- cascades to big_board_consensus rows.

Then re-run ``recompute_consensus`` to clear the original snapshot's
``rank_delta`` (or leave it for chart continuity).
"""

import asyncio
import importlib
import pkgutil
import random
from datetime import timedelta

import app.schemas

for _m in pkgutil.walk_packages(app.schemas.__path__, app.schemas.__name__ + "."):
    importlib.import_module(_m.name)

from sqlalchemy import text  # noqa: E402

from app.schemas.consensus import (  # noqa: E402
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
)
from app.utils.db_async import SessionLocal  # noqa: E402

random.seed(42)

DRAFT_YEAR = 2026
CURRENT_SNAPSHOT_ID = 1
WEEKS_BACK = [1, 2, 3, 4, 5]
# Random-walk std-dev per week; further-back snapshots are noisier so
# trajectories look organic rather than perfectly converging.
PERTURB_STD = {1: 1.5, 2: 2.5, 3: 3.5, 4: 4.5, 5: 5.5}


def _perturb_ranks(prior_ranks: dict[int, int], std: float) -> dict[int, int]:
    """Return a new ``{player_id: rank}`` after random-walk perturbation.

    Each player gets ``prior_rank + gaussian(0, std)``; we sort by the
    noisy score and re-assign unique ranks 1..N. This guarantees a valid
    ranking (no ties, no gaps) while preserving overall structure.
    """
    items = [(pid, rank + random.gauss(0, std)) for pid, rank in prior_ranks.items()]
    items.sort(key=lambda x: x[1])
    return {pid: i + 1 for i, (pid, _) in enumerate(items)}


async def main() -> None:
    async with SessionLocal() as db:
        # 1. Wipe only snapshots this script created on a prior run.
        # The sentinel `num_boards=0 AND trigger=MANUAL` is impossible for a
        # real `recompute_consensus` (which always references >=1 approved
        # board), so this never touches legitimate historical recomputes.
        await db.execute(
            text(
                "DELETE FROM consensus_snapshots WHERE draft_year=:y "
                "AND id <> :sid AND num_boards = 0 AND trigger = 'MANUAL'"
            ),
            {"y": DRAFT_YEAR, "sid": CURRENT_SNAPSHOT_ID},
        )

        # 2. Anchor on the current real snapshot — we only need its
        # computed_at to backdate the synthetic ones relative to it.
        snap1_at = (
            await db.execute(
                text("SELECT computed_at FROM consensus_snapshots WHERE id=:s"),
                {"s": CURRENT_SNAPSHOT_ID},
            )
        ).scalar_one()

        rows = (
            await db.execute(
                text(
                    "SELECT player_id, consensus_rank FROM big_board_consensus "
                    "WHERE snapshot_id=:s ORDER BY consensus_rank"
                ),
                {"s": CURRENT_SNAPSHOT_ID},
            )
        ).all()
        current_ranks: dict[int, int] = {pid: rank for pid, rank in rows}
        n_players = len(current_ranks)

        # 3. Walk backwards through time, perturbing each step.
        synthetic_ranks: dict[int, dict[int, int]] = {}
        prior_ranks = current_ranks
        for weeks in WEEKS_BACK:
            new_ranks = _perturb_ranks(prior_ranks, PERTURB_STD[weeks])
            synthetic_ranks[weeks] = new_ranks
            prior_ranks = new_ranks

        # 4. Insert oldest-first so prev_rank / rank_delta chain forward.
        prev_snap_ranks: dict[int, int] | None = None
        for weeks in sorted(WEEKS_BACK, reverse=True):
            ranks = synthetic_ranks[weeks]
            snap = ConsensusSnapshot(
                draft_year=DRAFT_YEAR,
                computed_at=snap1_at - timedelta(weeks=weeks),
                # Sentinel: real recomputes always reference >=1 approved
                # board, so num_boards=0 + board_ids=[] uniquely marks this
                # snapshot as synthetic for the cleanup query above.
                num_boards=0,
                board_ids=[],
                trigger=ConsensusTrigger.MANUAL,
            )
            db.add(snap)
            await db.flush()
            assert snap.id is not None

            for pid, rank in ranks.items():
                prev_rank = prev_snap_ranks.get(pid) if prev_snap_ranks else None
                rank_delta = (prev_rank - rank) if prev_rank is not None else None
                bbc = BigBoardConsensus(
                    snapshot_id=snap.id,
                    draft_year=DRAFT_YEAR,
                    player_id=pid,
                    consensus_rank=rank,
                    avg_rank=round(rank + random.uniform(-0.3, 0.3), 1),
                    median_rank=float(rank),
                    high_rank=max(1, rank - random.randint(0, 2)),
                    low_rank=min(n_players + 5, rank + random.randint(0, 3)),
                    std_dev=round(random.uniform(0.5, 2.0), 2),
                    num_sources=3,
                    prev_rank=prev_rank,
                    rank_delta=rank_delta,
                )
                db.add(bbc)
            prev_snap_ranks = ranks
        await db.flush()

        # 5. Refresh the current snapshot's prev_rank / rank_delta vs T-1w.
        t_minus_1 = synthetic_ranks[1]
        for pid, rank in current_ranks.items():
            prev_rank = t_minus_1.get(pid)
            rank_delta = (prev_rank - rank) if prev_rank is not None else None
            await db.execute(
                text(
                    "UPDATE big_board_consensus SET prev_rank=:pr, rank_delta=:rd "
                    "WHERE snapshot_id=:s AND player_id=:pid"
                ),
                {
                    "pr": prev_rank,
                    "rd": rank_delta,
                    "s": CURRENT_SNAPSHOT_ID,
                    "pid": pid,
                },
            )

        await db.commit()

    oldest = (snap1_at - timedelta(weeks=5)).date()
    newest_synth = (snap1_at - timedelta(weeks=1)).date()
    print(
        f"BACKFILL done: 5 synthetic snapshots ({oldest} → {newest_synth}); "
        f"snapshot {CURRENT_SNAPSHOT_ID} prev_rank/rank_delta updated vs T-1w."
    )


if __name__ == "__main__":
    asyncio.run(main())

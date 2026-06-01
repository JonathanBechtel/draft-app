"""Read/write helpers for the canonical draft-order reference.

The draft order (team per overall pick, including traded picks) is static,
public data maintained once per draft year. ``get_draft_order`` is the read
primitive consumed by the mock-draft presentation join; ``bulk_replace_draft_order``
loads/replaces a year's order wholesale (used by the seed script and, later, an
admin "paste the official order" action).

These functions are stateless and take ``AsyncSession`` first; callers own the
commit, except ``bulk_replace_draft_order`` which manages its own transaction
because it performs a delete-then-insert that must be atomic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.draft_pick_slots import DraftPickSlot


@dataclass
class PickSlotInput:
    """One pick's worth of draft-order data for ``bulk_replace_draft_order``.

    Teams are referenced by ``team_id`` (resolved by the caller against
    ``nba_teams``) so this layer stays free of name-matching concerns.
    """

    overall_pick: int
    round: int
    round_pick: int
    team_id: int
    original_team_id: Optional[int] = None
    trade_note: Optional[str] = None


async def get_draft_order(
    db: AsyncSession,
    *,
    draft_year: int,
) -> list[DraftPickSlot]:
    """Return all pick slots for a draft year, ordered by ``overall_pick``.

    Args:
        db: Async DB session.
        draft_year: The draft class to query.

    Returns:
        Pick slots ordered ascending by overall pick. Empty list when the
        year's order has not been seeded.
    """
    rows = (
        (
            await db.execute(
                select(DraftPickSlot)  # type: ignore[call-overload]
                .where(DraftPickSlot.draft_year == draft_year)  # type: ignore[arg-type]
                .order_by(DraftPickSlot.overall_pick)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def bulk_replace_draft_order(
    db: AsyncSession,
    *,
    draft_year: int,
    slots: list[PickSlotInput],
) -> int:
    """Replace the entire pick order for a draft year.

    Deletes any existing slots for ``draft_year`` then stages ``slots`` so the
    year's order is swapped wholesale. Idempotent: running the same input twice
    yields the same final state. The caller owns the commit (per the service
    convention); the delete and inserts land in the caller's transaction so the
    swap is atomic.

    Args:
        db: Async DB session.
        draft_year: The draft class to replace.
        slots: The full set of pick slots for the year.

    Returns:
        The number of slots staged.
    """
    now = datetime.utcnow()
    # Core DELETE executes immediately within the transaction, so the old rows
    # are gone before the new ones flush — no unique-constraint collision.
    await db.execute(
        delete(DraftPickSlot).where(
            DraftPickSlot.draft_year == draft_year  # type: ignore[arg-type]
        )
    )
    await db.flush()
    for s in slots:
        db.add(
            DraftPickSlot(
                draft_year=draft_year,
                overall_pick=s.overall_pick,
                round=s.round,
                round_pick=s.round_pick,
                team_id=s.team_id,
                original_team_id=s.original_team_id,
                trade_note=s.trade_note,
                created_at=now,
                updated_at=now,
            )
        )
    await db.flush()
    return len(slots)

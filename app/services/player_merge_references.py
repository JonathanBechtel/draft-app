"""The safe-delete guard: how many rows still point at a player.

Split out of ``player_merge_service`` (#681), which was already over the file-size
ratchet (``docs/plans/programmatic-code-discipline.md`` §1.4). This is a read-only
question — *would deleting this player break a foreign key?* — asked by
``admin_player_service.delete_stub`` before it removes a stub, and it is derived
entirely from the child-table registry, so it has no dependency on the merge
machinery itself.

Deriving it from the classified merge specs is what keeps the guard from drifting
away from the merge path again (#675): every reassignable ``(table, column)`` pair
is by construction a non-CASCADE inbound FK, and a table registered for
reassignment is exactly a table whose rows would block a delete.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.player_merge_tables import (
    _CHILD_TABLES,
    _SIMILARITY_ANCHOR,
    _SIMILARITY_COMPARISON,
    _SPEC_TABLE_ALIASES,
)


_INBOUND_REFERENCE_SPECS: tuple[tuple[str, str], ...] = tuple(
    (_SPEC_TABLE_ALIASES.get(spec.table, spec.table), spec.player_column)
    for spec in (*_CHILD_TABLES, _SIMILARITY_ANCHOR, _SIMILARITY_COMPARISON)
)

_INBOUND_REFERENCE_LABELS: tuple[str, ...] = tuple(
    f"{table}.{column}" for table, column in _INBOUND_REFERENCE_SPECS
)

# One statement rather than one per spec (#681): the registry is 30+ entries and
# bulk stub deletion runs the guard once per selected player, so a query apiece
# meant hundreds of sequential round trips to Postgres for what is a handful of
# index lookups. Names come from the registry (trusted constants), and every
# referenced column carries a player-leading index — enforced by
# tests/unit/test_player_merge_index_coverage.py.
_INBOUND_REFERENCE_SQL: str = "\nUNION ALL\n".join(
    f"SELECT '{table}.{column}' AS label, count(*) AS n"
    f" FROM {table} WHERE {column} = :player_id"
    for table, column in _INBOUND_REFERENCE_SPECS
)


async def count_inbound_references(
    db: AsyncSession,
    player_id: int,
) -> dict[str, int]:
    """Return a per-table count of all inbound FK references for a player.

    Used by the safe-delete guard to block deletion of players that still have
    attached data.  Only counts non-CASCADE tables (CASCADE tables drop
    automatically and are not a deletion blocker).

    Issued as a single ``UNION ALL`` statement over the classified merge specs
    (:data:`_INBOUND_REFERENCE_SQL`) — one round trip per player rather than one
    per registered table.

    Args:
        db: Active async database session.
        player_id: Player to inspect.

    Returns:
        Dict mapping table.column label to row count (only non-zero entries are
        included), in registry order.
    """
    rows = (
        await db.execute(text(_INBOUND_REFERENCE_SQL), {"player_id": player_id})
    ).all()
    counts_by_label = {str(label): int(n or 0) for label, n in rows}
    # Rebuild in registry order rather than result order: UNION ALL does not
    # guarantee row order, and the labels end up in delete_stub's user-facing
    # refusal message.
    return {
        label: counts_by_label[label]
        for label in _INBOUND_REFERENCE_LABELS
        if counts_by_label.get(label)
    }

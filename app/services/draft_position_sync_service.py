"""Propagate draft-night outcomes into the player master's draft columns.

``draft_results`` is the authoritative record of what happened on draft night,
but the Summer League Explorer (and its draft-class facet) filter on
``players_master.draft_year / draft_round / draft_pick`` — columns that were
historically populated only by the Basketball-Reference bio scraper. Nothing
bridged the two, so a freshly ingested class (e.g. 2026) had picks in
``draft_results`` yet NULL ``draft_round`` / ``draft_pick`` on the player, and
the Explorer's draft filters silently dropped every one of them.

This module is that bridge. It copies ``round`` / ``round_pick`` (and the
selecting team's abbreviation) from every *resolved* ``draft_results`` row onto
the matched ``players_master`` record. ``draft_results`` is treated as
authoritative for the columns it owns, so the sync is idempotent and safe to
re-run. It is invoked automatically at the end of ``ingest_draft_results.py``.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Bulk UPDATE ... FROM: for every resolved pick (player_id set), stamp the
# player's within-round draft position from draft_results. draft_results.round
# -> draft_round and draft_results.round_pick -> draft_pick both match
# players_master's within-round semantics. draft_team takes the selecting team's
# abbreviation, falling back to the raw token, then the existing value.
_SYNC_SQL = text(
    """
    UPDATE players_master AS pm
    SET draft_year = dr.draft_year,
        draft_round = dr.round,
        draft_pick = dr.round_pick,
        draft_team = COALESCE(nt.abbreviation, dr.raw_team, pm.draft_team)
    FROM draft_results AS dr
    LEFT JOIN nba_teams AS nt ON nt.id = dr.team_id
    WHERE pm.id = dr.player_id
      AND dr.player_id IS NOT NULL
      AND (
        CAST(:draft_year AS INTEGER) IS NULL
        OR dr.draft_year = CAST(:draft_year AS INTEGER)
      )
    """
)


async def sync_draft_positions(
    db: AsyncSession, *, draft_year: Optional[int] = None
) -> int:
    """Copy resolved ``draft_results`` picks into ``players_master.draft_*``.

    Only rows whose pick resolved to a player (``player_id`` set) are synced;
    unresolved picks and undrafted prospects are left untouched, so an
    undrafted player projected to a class keeps his NULL round/pick. The caller
    owns the transaction — this does not commit.

    Args:
        db: Active async session.
        draft_year: Restrict the sync to a single draft year; ``None`` syncs
            every year present in ``draft_results``.

    Returns:
        The number of ``players_master`` rows updated.
    """
    result = await db.execute(_SYNC_SQL, {"draft_year": draft_year})
    # execute() is typed as Result, but a DML statement returns a CursorResult
    # at runtime, which carries the affected-row count.
    return result.rowcount or 0  # type: ignore[attr-defined]

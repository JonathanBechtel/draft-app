"""Integration tests for the C5 nba_stats external-id backfill sweep.

Covers the invariants that make the sweep safe to run repeatedly over the
multi-year resolved Summer League cohort:

1. Resolved source players are seeded; unresolved ones are skipped.
2. The sweep is idempotent (a second run seeds nothing).
3. A PERSON_ID already linked to a *different* player is reported as a conflict
   rather than crashing the sweep.

Requires ``TEST_DATABASE_URL`` and ``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
)
from app.services.sources.summer_league.player_resolution import (
    NBA_STATS_SYSTEM,
    backfill_nba_stats_external_ids,
)


def _make_player(display_name: str) -> PlayerMaster:
    parts = display_name.split(" ", 1)
    return PlayerMaster(
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        display_name=display_name,
        is_stub=False,
        bio_source="test",
    )


def _make_source_player(
    person_id: str,
    name: str,
    canonical_player_id: int | None,
) -> SummerLeagueSourceRecord:
    status = (
        SummerLeagueResolutionStatus.EXTERNAL_ID
        if canonical_player_id is not None
        else SummerLeagueResolutionStatus.UNRESOLVED
    )
    return SummerLeagueSourceRecord(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        normalized_name=name.lower(),
        first_seen_year=2025,
        last_seen_year=2026,
        canonical_player_id=canonical_player_id,
        resolution_status=status,
    )


@pytest.mark.asyncio
async def test_backfill_seeds_resolved_and_skips_unresolved(
    db_session: AsyncSession,
) -> None:
    """Resolved players are seeded; unresolved players are left alone; idempotent."""
    resolved = _make_player("Resolved Rookie")
    db_session.add(resolved)
    await db_session.flush()
    assert resolved.id is not None

    db_session.add(_make_source_player("1640100", "Resolved Rookie", resolved.id))
    # Unresolved: no canonical id → must be skipped.
    db_session.add(_make_source_player("1640200", "Unknown Prospect", None))
    await db_session.flush()

    report = await backfill_nba_stats_external_ids(db_session)

    assert report.seeded == 1
    assert report.already_present == 0
    assert report.conflicts == []

    rows = (
        (
            await db_session.execute(
                select(PlayerExternalId).where(
                    PlayerExternalId.system == NBA_STATS_SYSTEM
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].player_id == resolved.id
    assert rows[0].external_id == "1640100"

    # Second pass seeds nothing (idempotent).
    second = await backfill_nba_stats_external_ids(db_session)
    assert second.seeded == 0
    assert second.already_present == 1
    total = await db_session.scalar(select(func.count()).select_from(PlayerExternalId))
    assert total == 1


@pytest.mark.asyncio
async def test_backfill_reports_conflict_without_crashing(
    db_session: AsyncSession,
) -> None:
    """A PERSON_ID already linked to a different player is reported, not raised."""
    player_a = _make_player("Player A")
    player_b = _make_player("Player B")
    db_session.add(player_a)
    db_session.add(player_b)
    await db_session.flush()
    assert player_a.id is not None and player_b.id is not None

    # Pre-existing external id links PERSON_ID 1640400 to player A.
    db_session.add(
        PlayerExternalId(
            player_id=player_a.id,
            system=NBA_STATS_SYSTEM,
            external_id="1640400",
        )
    )
    # A resolved source player claims the same PERSON_ID belongs to player B.
    db_session.add(_make_source_player("1640400", "Player B", player_b.id))
    await db_session.flush()

    report = await backfill_nba_stats_external_ids(db_session)

    assert report.seeded == 0
    assert report.conflicts == [("1640400", player_a.id, player_b.id)]
    # The original link is untouched.
    linked = (
        await db_session.execute(
            select(PlayerExternalId).where(
                PlayerExternalId.system == NBA_STATS_SYSTEM,
                PlayerExternalId.external_id == "1640400",
            )
        )
    ).scalar_one()
    assert linked.player_id == player_a.id

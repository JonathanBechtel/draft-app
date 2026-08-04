"""Queries for roster changes produced by one normalized roster snapshot."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import AffiliationStatus, PlayerAffiliation
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
)


async def changed_source_player_ids(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    recorded_at: datetime,
) -> set[int]:
    """Return source players whose roster assertion was added in this load."""
    result = await db.execute(
        select(SummerLeagueParticipation.source_player_id)  # type: ignore[call-overload]
        .join(
            PlayerAffiliation,
            SummerLeagueParticipation.affiliation_id == PlayerAffiliation.id,
        )
        .join(
            SummerLeagueEdition,
            SummerLeagueParticipation.competition_id == SummerLeagueEdition.id,
        )
        .where(
            SummerLeagueEdition.year == year,  # type: ignore[arg-type]
            SummerLeagueEdition.league_id == league_id,
            PlayerAffiliation.recorded_at == recorded_at,
            PlayerAffiliation.status.in_(  # type: ignore[attr-defined]
                [AffiliationStatus.ANNOUNCED, AffiliationStatus.CUT]
            ),
        )
    )
    return {source_player_id for (source_player_id,) in result.all()}


async def canonical_player_ids(
    db: AsyncSession, source_player_ids: set[int]
) -> set[int]:
    """Resolve source-player IDs to canonical IDs after roster resolution."""
    if not source_player_ids:
        return set()
    result = await db.execute(
        select(SummerLeagueSourcePlayer.canonical_player_id).where(  # type: ignore[call-overload]
            SummerLeagueSourcePlayer.id.in_(  # type: ignore[attr-defined, union-attr]
                source_player_ids
            ),
        )
    )
    return {player_id for (player_id,) in result.all() if player_id is not None}

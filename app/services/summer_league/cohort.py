"""Shared read helper: the Summer League rostered cohort.

Returns the canonical ``player_id``s (and ``source_player_id``s) that have a
``summer_league_participation`` row, filterable by year / league_id / venue.
Every enrichment path (bio, college-stats, image-generation) targets this
cohort instead of scanning all players.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
)


@dataclass
class CohortResult:
    """The Summer League rostered cohort within a given scope.

    Attributes:
        player_ids: Resolved canonical players (``player_id`` NOT NULL).
        source_player_ids: All source players in scope, resolved or not.
    """

    player_ids: set[int] = field(default_factory=set)
    source_player_ids: set[int] = field(default_factory=set)


async def summer_league_cohort(
    db: AsyncSession,
    *,
    year: Optional[int] = None,
    league_id: Optional[str] = None,
    venue_slug: Optional[str] = None,
) -> CohortResult:
    """Return the Summer League rostered cohort for the given scope.

    Joins ``summer_league_participation`` to ``summer_league_competitions``
    so the year / league_id / venue filters apply to the competition a
    participation belongs to.

    Args:
        db: Active async session.
        year: Optional competition year filter.
        league_id: Optional NBA.com ``LeagueID`` filter.
        venue_slug: Optional venue slug filter.

    Returns:
        A :class:`CohortResult` with the resolved ``player_id``s and the full
        set of ``source_player_id``s in scope.
    """
    part = SummerLeagueParticipation
    comp = SummerLeagueEdition

    stmt = (
        select(part.player_id, part.source_player_id)  # type: ignore[call-overload]
        .select_from(part)
        .join(comp, comp.id == part.competition_id)  # type: ignore[arg-type]
    )
    if year is not None:
        stmt = stmt.where(comp.year == year)  # type: ignore[arg-type]
    if league_id is not None:
        stmt = stmt.where(comp.league_id == league_id)
    if venue_slug is not None:
        stmt = stmt.where(comp.venue_slug == venue_slug)

    rows = (await db.execute(stmt)).all()
    return CohortResult(
        player_ids={r.player_id for r in rows if r.player_id is not None},
        source_player_ids={r.source_player_id for r in rows},
    )

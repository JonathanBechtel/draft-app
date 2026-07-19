"""Typed scope/profile boundaries for Competition Context reads.

This module defines the stable :class:`EnvironmentScope` value object and the
current-profile lookup skeleton consumed by the Explorer read contract (#607)
and cross-surface reuse (#609/#610). It performs **no raw aggregation** — that
is #617's job. Reads always resolve exactly one *current* profile per scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import select
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile

ScopeKind = Literal["season_all_competitions", "competition"]

# The service returns the persisted ORM row as the profile boundary type. The
# alias keeps call sites reading against the contract's ``EnvironmentProfile``
# name without coupling to the table class spelling.
EnvironmentProfile = SummerLeagueEnvironmentProfile


@dataclass(frozen=True)
class EnvironmentScope:
    """A stable Competition Context scope identity.

    Attributes:
        scope_kind: ``season_all_competitions`` or ``competition``.
        year: The competition calendar year.
        competition_id: The canonical competition id for a competition scope;
            ``None`` for a season (all-competitions) scope.
        scope_key: The stable key ``season:<year>`` or
            ``competition:<competition_id>`` — never a display label.
    """

    scope_kind: ScopeKind
    year: int
    competition_id: Optional[int]
    scope_key: str

    @classmethod
    def for_season(cls, year: int) -> "EnvironmentScope":
        """Build the all-competitions season scope for a year."""
        return cls(
            scope_kind="season_all_competitions",
            year=year,
            competition_id=None,
            scope_key=season_scope_key(year),
        )

    @classmethod
    def for_competition(cls, competition_id: int, year: int) -> "EnvironmentScope":
        """Build the scope for one named competition edition."""
        return cls(
            scope_kind="competition",
            year=year,
            competition_id=competition_id,
            scope_key=competition_scope_key(competition_id),
        )


def season_scope_key(year: int) -> str:
    """Stable season scope key ``season:<year>``."""
    return f"season:{year}"


def competition_scope_key(competition_id: int) -> str:
    """Stable competition scope key ``competition:<competition_id>``."""
    return f"competition:{competition_id}"


async def get_environment_profile(
    db: AsyncSession, scope: EnvironmentScope
) -> Optional[EnvironmentProfile]:
    """Return the single *current* profile for a scope, or ``None``.

    Explicitly selects the one row flagged ``is_current`` for the scope key; a
    partial unique index guarantees at most one such row exists, so no ordering
    tie-break is needed. Returns ``None`` when no current profile is published
    yet (readers should then present a stale/empty state, never fabricate one).

    Args:
        db: Async session.
        scope: The stable scope identity to resolve.

    Returns:
        The current :class:`SummerLeagueEnvironmentProfile`, or ``None``.
    """
    result = await db.execute(
        select(SummerLeagueEnvironmentProfile).where(
            col(SummerLeagueEnvironmentProfile.scope_key) == scope.scope_key,
            col(SummerLeagueEnvironmentProfile.is_current).is_(True),
        )
    )
    return result.scalar_one_or_none()

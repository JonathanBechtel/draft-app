"""Dual-read resolution for ``PlayerAffiliation`` targets.

``PlayerAffiliation`` carries two nullable target columns during the phase-4
transition (journey-graph §7a, §13; phase-4 spec §5.1 decision D3):
``team_program_id``, the generic org-model target, and ``nba_team_id``, the
NBA-franchise target every Summer League row has always used. Both are
retained -- no row is ever repointed or nulled -- and every reader is expected
to go through :func:`resolve_affiliation_target` rather than open-coding an
``x or y`` fallback at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from app.schemas.player_affiliation import PlayerAffiliation


@dataclass(frozen=True)
class TeamProgramRef:
    """The affiliation resolved to a generic-org-model ``TeamProgram``."""

    team_program_id: int


@dataclass(frozen=True)
class NbaTeamRef:
    """The affiliation resolved to a legacy ``NbaTeam`` (no program known yet)."""

    nba_team_id: int


AffiliationTargetRef = Union[TeamProgramRef, NbaTeamRef]


def resolve_affiliation_target(
    affiliation: PlayerAffiliation,
) -> Optional[AffiliationTargetRef]:
    """Return the affiliation's resolved target, preferring the generic org model.

    Args:
        affiliation: The assertion to resolve. Only its ``team_program_id`` and
            ``nba_team_id`` fields are read.

    Returns:
        A :class:`TeamProgramRef` when ``team_program_id`` is set, else an
        :class:`NbaTeamRef` when ``nba_team_id`` is set, else ``None`` when
        neither target is known (e.g. an unresolved roster row).
    """
    if affiliation.team_program_id is not None:
        return TeamProgramRef(team_program_id=affiliation.team_program_id)
    if affiliation.nba_team_id is not None:
        return NbaTeamRef(nba_team_id=affiliation.nba_team_id)
    return None

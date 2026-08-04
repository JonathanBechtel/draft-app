"""Dual-read resolution for team-target rows across the journey-graph transition.

Both ``PlayerAffiliation`` and ``SummerLeagueTeamEntry`` carry the same pair of
nullable target columns during the phase-4 transition (journey-graph §7a, §13;
phase-4 spec §5.1 decision D3): ``team_program_id``, the generic org-model
target, and ``nba_team_id``, the NBA-franchise target every Summer League row
has always used. Both are retained on every table -- no row is ever repointed
or nulled -- and every reader is expected to go through
:func:`resolve_team_target` rather than open-coding an ``x or y`` fallback at
the call site.

``resolve_team_target`` is structurally typed (``_DualTeamTarget``) so the one
function serves any row shaped this way without forking; T4 (#783) introduced
it as ``resolve_affiliation_target`` for ``PlayerAffiliation`` alone, and T-784
generalized it for ``SummerLeagueTeamEntry`` rather than writing a second
helper. ``resolve_affiliation_target`` is kept as an alias so existing
call sites and tests are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Union, runtime_checkable


@runtime_checkable
class _DualTeamTarget(Protocol):
    """Structural shape shared by every dual-read team-target row."""

    team_program_id: Optional[int]
    nba_team_id: Optional[int]


@dataclass(frozen=True)
class TeamProgramRef:
    """The row resolved to a generic-org-model ``TeamProgram``."""

    team_program_id: int


@dataclass(frozen=True)
class NbaTeamRef:
    """The row resolved to a legacy ``NbaTeam`` (no program known yet)."""

    nba_team_id: int


AffiliationTargetRef = Union[TeamProgramRef, NbaTeamRef]


def resolve_team_target(entity: _DualTeamTarget) -> Optional[AffiliationTargetRef]:
    """Return a dual-read row's resolved target, preferring the generic org model.

    Args:
        entity: The row to resolve. Only its ``team_program_id`` and
            ``nba_team_id`` fields are read, so any row with that structural
            shape works (``PlayerAffiliation``, ``SummerLeagueTeamEntry``).

    Returns:
        A :class:`TeamProgramRef` when ``team_program_id`` is set, else an
        :class:`NbaTeamRef` when ``nba_team_id`` is set, else ``None`` when
        neither target is known (e.g. an unresolved roster row).
    """
    if entity.team_program_id is not None:
        return TeamProgramRef(team_program_id=entity.team_program_id)
    if entity.nba_team_id is not None:
        return NbaTeamRef(nba_team_id=entity.nba_team_id)
    return None


# Back-compat alias: T4 (#783) shipped this name for PlayerAffiliation
# resolution specifically. Keep it pointed at the now-generalized function so
# existing imports keep working unchanged.
resolve_affiliation_target = resolve_team_target

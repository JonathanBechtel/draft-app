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

Two readers, one rule (#795)
----------------------------
A row-at-a-time resolver is only half of what a real reader needs. A page that
*selects* the rows belonging to one team cannot resolve first and filter after
-- it has to express the same preference rule as a ``WHERE`` clause, in SQL,
before any row exists in Python. Left to itself every such call site would
open-code that clause, which is the exact drift this module was written to
prevent (a plain ``team_program_id = :p OR nba_team_id = :n`` is the natural
thing to write and it is *wrong*: it also matches a row already retargeted to
a different program that still carries its legacy ``nba_team_id``).

:func:`resolve_team_target_filter` is therefore shipped alongside as the SQL
projection of :func:`resolve_team_target`: it selects exactly the rows the
resolver maps to one of the caller's targets, and nothing else. The two are
kept honest by ``tests/integration/test_summer_league_franchise.py``, which
seeds the full ``(team_program_id, nba_team_id)`` truth table in Postgres and
asserts the clause returns precisely the set :func:`resolve_team_target`
selects in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Union, runtime_checkable

from sqlalchemy import and_, false, or_
from sqlalchemy.sql.elements import ColumnElement


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


def resolve_team_target_filter(
    entity: Any,
    *,
    team_program_id: Optional[int],
    nba_team_id: Optional[int],
) -> ColumnElement[bool]:
    """Return the SQL clause selecting rows :func:`resolve_team_target` maps here.

    The set-at-a-time twin of :func:`resolve_team_target`, for readers that must
    filter in the database rather than resolve a row already in hand. A row is
    selected when its resolved target -- *as the resolver defines it*, program
    first -- equals one of the targets passed in:

    - ``team_program_id = :team_program_id``: the row resolves to a
      :class:`TeamProgramRef`, so its ``nba_team_id`` is irrelevant.
    - ``team_program_id IS NULL AND nba_team_id = :nba_team_id``: the row has no
      program yet, so the resolver falls back to :class:`NbaTeamRef`.

    The ``IS NULL`` term on the second branch is the load-bearing part and the
    one a hand-written ``OR`` drops. Without it the clause also matches a row
    that has already been retargeted to a *different* program while retaining
    its legacy ``nba_team_id`` -- a row the resolver assigns to that other
    program, which would then appear under two different teams at once.

    Args:
        entity: The mapped class (or aliased class) to filter, e.g.
            ``SummerLeagueTeamEntry``. Only its ``team_program_id`` and
            ``nba_team_id`` columns are referenced.
        team_program_id: The caller's generic-org-model target, or ``None``
            when the org model has no program for it yet (pre-population, or
            an organization too ambiguous to resolve). ``None`` drops the
            program branch entirely rather than matching ``IS NULL``, which
            would sweep in every unpopulated row in the table.
        nba_team_id: The caller's legacy NBA-franchise target, or ``None`` for
            a non-NBA caller with no franchise identity.

    Returns:
        A boolean clause for ``.where()``. With both arguments ``None`` there
        is no target to match and the clause is a constant false, so the caller
        gets an empty result rather than the whole table.
    """
    branches: list[ColumnElement[bool]] = []
    if team_program_id is not None:
        branches.append(entity.team_program_id == team_program_id)
    if nba_team_id is not None:
        branches.append(
            and_(
                entity.team_program_id.is_(None),
                entity.nba_team_id == nba_team_id,
            )
        )
    if not branches:
        return false()
    return or_(*branches)


# Back-compat alias: T4 (#783) shipped this name for PlayerAffiliation
# resolution specifically. Keep it pointed at the now-generalized function so
# existing imports keep working unchanged.
resolve_affiliation_target = resolve_team_target

"""Shared franchise -> team_program map builder for the phase-4 backfill scripts.

Ticket #799. ``scripts/backfill_affiliation_team_program.py`` and
``scripts/backfill_sl_team_entry_team_program.py`` both need the same bulk
bridge: ``nba_teams.id`` -> ``nba_teams.slug`` -> ``organizations.slug`` (via
``derive_org_slug``) -> ``organizations.id`` -> the ``team_programs`` row(s)
owned by that organization -> ``team_programs.id``.

Before this ticket, each script built that bridge with its own
``organization_id``-keyed dict comprehension and no ``ORDER BY`` -- when an
organization owned more than one ``team_programs`` row, whichever row the
query happened to return last silently won, and not even stably across runs.
This module replaces both copies with one function that delegates the
ambiguity guard to
``app.services.backbone.team_program_resolution.build_franchise_team_program_map``
(#796): an organization with more than one program raises
``AmbiguousTeamProgramError`` instead of guessing.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, TeamProgram
from app.services.backbone.team_program_resolution import (
    TeamProgramRow,
    build_franchise_team_program_map,
    derive_org_slug,
)


async def franchise_nba_team_id_to_team_program_id(db: AsyncSession) -> dict[int, int]:
    """Return ``{nba_team_id: team_program_id}`` for every resolvable franchise.

    Reuses the T3 natural key (``derive_org_slug``) and the #796 ambiguity
    guard (``build_franchise_team_program_map``) rather than re-deriving
    either, so the two backfill scripts calling this can never silently
    diverge on how an NBA team maps to its organization/team_program pair --
    and can never silently pick an arbitrary program for an organization that
    owns more than one.

    Args:
        db: Active database session (read-only; issues no writes).

    Returns:
        A map from ``nba_teams.id`` to the ``team_programs.id`` T3 created for
        that franchise. Empty when the org model hasn't been populated yet.

    Raises:
        AmbiguousTeamProgramError: If any franchise organization owns more
            than one ``team_programs`` row -- per this repo's
            entity-resolution rule, an ambiguous target is refused, never
            guessed.
    """
    teams = (
        await db.execute(
            select(NbaTeam.id, NbaTeam.slug)  # type: ignore[call-overload]
            .select_from(NbaTeam)
            .order_by(NbaTeam.id)
        )
    ).all()
    if not teams:
        return {}

    org_slug_to_nba_team_id = {
        derive_org_slug(slug): team_id for team_id, slug in teams
    }

    org_rows = (
        await db.execute(
            select(Organization.id, Organization.slug).where(  # type: ignore[call-overload]
                Organization.slug.in_(org_slug_to_nba_team_id)  # type: ignore[attr-defined]
            )
        )
    ).all()
    organization_id_to_nba_team_id = {
        organization_id: org_slug_to_nba_team_id[org_slug]
        for organization_id, org_slug in org_rows
    }
    if not organization_id_to_nba_team_id:
        return {}

    program_rows = [
        TeamProgramRow(
            team_program_id=program_id,
            team_program_slug=program_slug,
            organization_id=organization_id,
        )
        for program_id, program_slug, organization_id in (
            await db.execute(
                select(  # type: ignore[call-overload]
                    TeamProgram.id, TeamProgram.slug, TeamProgram.organization_id
                ).where(
                    TeamProgram.organization_id.in_(  # type: ignore[attr-defined]
                        organization_id_to_nba_team_id
                    )
                )
            )
        ).all()
    ]

    # Raises AmbiguousTeamProgramError if any organization above owns more
    # than one team_programs row. The slug-keyed map this returns is
    # discarded -- what matters here is the validation, since both callers
    # need the nba_team_id-keyed shape built below.
    build_franchise_team_program_map(program_rows)

    return {
        organization_id_to_nba_team_id[row.organization_id]: row.team_program_id
        for row in program_rows
        if row.organization_id in organization_id_to_nba_team_id
    }

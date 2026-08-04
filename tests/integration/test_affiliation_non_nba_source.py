"""The phase-4 exit-criterion proof: a non-NBA source can assert an affiliation.

Ticket #783. Per phase-4 spec §5.1 (exit criterion 2), this is *the* mechanical
proof of the headline exit criterion, not an inspection: create a FEDERATION
``Organization``, a ``TeamProgram`` owned by it, then a ``PlayerAffiliation``
targeting that program with ``nba_team_id`` NULL. Before ``team_program_id``
existed, this affiliation had no way to name a non-NBA target at all -- every
``PlayerAffiliation`` was implicitly NBA-only.

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.organization import Organization, OrgKind, TeamProgram
from app.schemas.player_affiliation import (
    AffiliationType,
    PlayerAffiliation,
)
from app.services.player_affiliation import (
    TeamProgramRef,
    resolve_affiliation_target,
)


async def _make_federation_program(db: AsyncSession) -> TeamProgram:
    """Seed a FEDERATION organization and a team_program it owns."""
    federation = Organization(
        org_kind=OrgKind.FEDERATION,
        name="Test Federation",
        slug="test-federation-non-nba",
    )
    db.add(federation)
    await db.flush()
    assert federation.id is not None

    program = TeamProgram(
        organization_id=federation.id,
        name="Test Federation U18 National Team",
        slug="test-federation-non-nba-u18",
        level="U18",
    )
    db.add(program)
    await db.flush()
    assert program.id is not None
    return program


@pytest.mark.asyncio
async def test_non_nba_source_can_assert_an_affiliation(
    db_session: AsyncSession,
) -> None:
    """A federation-owned program can be an affiliation target with nba_team_id NULL.

    This is exit criterion 2 made mechanical: it fails if `team_program_id` is
    ever removed from `PlayerAffiliation`, because there would be nothing for
    a non-NBA-sourced assertion to target.
    """
    program = await _make_federation_program(db_session)
    program_id = program.id

    affiliation = PlayerAffiliation(
        player_id=None,
        team_program_id=program_id,
        nba_team_id=None,
        affiliation_type=AffiliationType.NATIONAL_TEAM,
        source="test_non_nba_federation_source",
        source_ref="fiba/u18/roster-entry-1",
    )
    db_session.add(affiliation)
    await db_session.commit()

    persisted = (
        await db_session.execute(
            select(PlayerAffiliation).where(
                PlayerAffiliation.source == "test_non_nba_federation_source"
            )
        )
    ).scalar_one()

    assert persisted.team_program_id == program_id
    assert persisted.nba_team_id is None

    resolved = resolve_affiliation_target(persisted)
    assert resolved == TeamProgramRef(team_program_id=program_id)

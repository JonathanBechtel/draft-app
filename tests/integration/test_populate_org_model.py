"""Integration tests for the org-model-from-nba_teams population script.

Seeds ``nba_teams`` rows, runs the population, and asserts one organization
(kind ``CLUB``) plus one team_program per team is created. The idempotency
property is the one most likely to silently break, so the population is run
a second time and asserted to create nothing. ``--dry-run`` (``run_population``
with ``dry_run=True``) is also asserted to create nothing while reporting the
same pending count the real run then produces.

These tests run against the live integration-test Postgres schema (via
db_session from conftest), so they require TEST_DATABASE_URL and
PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, OrgKind, TeamProgram
from scripts.populate_org_model_from_nba_teams import run_population


async def _seed_nba_teams(db: AsyncSession, count: int = 3) -> list[NbaTeam]:
    """Seed a handful of nba_teams rows for the population to convert."""
    teams = [
        NbaTeam(name=f"Test Team {i}", abbreviation=f"TT{i}", slug=f"test-team-{i}")
        for i in range(count)
    ]
    db.add_all(teams)
    await db.commit()
    return teams


@pytest.mark.asyncio
async def test_population_creates_one_org_and_program_per_team(
    db_session: AsyncSession,
) -> None:
    """Each nba_teams row yields exactly one CLUB organization and one program."""
    await _seed_nba_teams(db_session, count=3)

    report = await run_population(db_session)

    assert report.planned == 3
    assert report.organizations_created == 3
    assert report.organizations_skipped == 0
    assert report.team_programs_created == 3
    assert report.team_programs_skipped == 0
    assert report.failed == 0

    org_count = await db_session.scalar(
        select(func.count()).select_from(Organization)
    )
    program_count = await db_session.scalar(
        select(func.count()).select_from(TeamProgram)
    )
    assert org_count == 3
    assert program_count == 3

    orgs = (
        await db_session.execute(select(Organization).order_by(Organization.slug))
    ).scalars().all()
    for org in orgs:
        assert org.org_kind == OrgKind.CLUB
        assert org.slug.startswith("nba-test-team-")

    programs = (
        await db_session.execute(select(TeamProgram).order_by(TeamProgram.slug))
    ).scalars().all()
    org_ids = {org.id for org in orgs}
    for program in programs:
        assert program.organization_id in org_ids
        assert program.slug.startswith("nba-test-team-")


@pytest.mark.asyncio
async def test_population_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """Running the population twice creates nothing the second time."""
    await _seed_nba_teams(db_session, count=4)

    first = await run_population(db_session)
    assert first.organizations_created == 4
    assert first.team_programs_created == 4
    assert first.failed == 0

    second = await run_population(db_session)

    assert second.planned == 4
    assert second.organizations_created == 0
    assert second.organizations_skipped == 4
    assert second.team_programs_created == 0
    assert second.team_programs_skipped == 4
    assert second.failed == 0

    org_count = await db_session.scalar(
        select(func.count()).select_from(Organization)
    )
    program_count = await db_session.scalar(
        select(func.count()).select_from(TeamProgram)
    )
    assert org_count == 4
    assert program_count == 4


@pytest.mark.asyncio
async def test_dry_run_creates_nothing_and_matches_the_real_run_count(
    db_session: AsyncSession,
) -> None:
    """--dry-run reports the same pending count the real run then creates."""
    await _seed_nba_teams(db_session, count=5)

    dry_report = await run_population(db_session, dry_run=True)

    assert dry_report.planned == 5
    assert dry_report.organizations_created == 5
    assert dry_report.team_programs_created == 5

    org_count = await db_session.scalar(
        select(func.count()).select_from(Organization)
    )
    program_count = await db_session.scalar(
        select(func.count()).select_from(TeamProgram)
    )
    assert org_count == 0
    assert program_count == 0

    real_report = await run_population(db_session)

    assert real_report.organizations_created == dry_report.organizations_created
    assert real_report.team_programs_created == dry_report.team_programs_created

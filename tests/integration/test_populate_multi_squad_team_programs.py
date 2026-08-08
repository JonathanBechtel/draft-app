"""Integration tests for the multi-squad team_program population script (#810).

Seeds real NBA franchise ``nba_teams`` rows for the four multi-squad
franchises (Golden State Warriors, Orlando Magic, Sacramento Kings, Utah
Jazz), runs T3's population first (as an operator would), then runs this
ticket's sibling population and asserts: the organization stays exactly one
per franchise, the sibling ``team_programs`` rows land under it (never a new
organization), idempotency on rerun, and that a franchise whose T3 population
has not run yet is reported via ``organization_missing`` rather than crashing
the run.

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, TeamProgram
from scripts.populate_multi_squad_team_programs import run_population
from scripts.populate_org_model_from_nba_teams import (
    run_population as run_primary_population,
)


async def _seed_multi_squad_franchises(db: AsyncSession) -> None:
    """Seed the four real multi-squad franchises plus a decoy (Lakers).

    The Lakers decoy means a resolver that grabs "any" organization's
    programs cannot pass -- only one keyed on the exact franchise slug can.
    """
    db.add_all(
        [
            NbaTeam(name="Golden State Warriors", abbreviation="GSW", slug="warriors"),
            NbaTeam(name="Orlando Magic", abbreviation="ORL", slug="magic"),
            NbaTeam(name="Sacramento Kings", abbreviation="SAC", slug="kings"),
            NbaTeam(name="Utah Jazz", abbreviation="UTA", slug="jazz"),
            NbaTeam(name="Los Angeles Lakers", abbreviation="LAL", slug="lakers"),
        ]
    )
    await db.commit()


@pytest.mark.asyncio
async def test_population_creates_two_sibling_programs_per_multi_squad_franchise(
    db_session: AsyncSession,
) -> None:
    """Each of the four franchises gains exactly two additional programs.

    The organization count must not change -- no new organization is
    created, and the Lakers (no sibling squads) gain nothing.
    """
    await _seed_multi_squad_franchises(db_session)
    primary_report = await run_primary_population(db_session)
    assert primary_report.failed == 0

    org_count_before = await db_session.scalar(
        select(func.count()).select_from(Organization)
    )

    report = await run_population(db_session)

    assert report.planned == 8
    assert report.team_programs_created == 8
    assert report.team_programs_skipped == 0
    assert report.organization_missing == 0
    assert report.failed == 0

    db_session.expunge_all()
    org_count_after = await db_session.scalar(
        select(func.count()).select_from(Organization)
    )
    assert org_count_after == org_count_before  # no squad collapsed onto a new org

    # Raw SQL per this repo's anti-vacuity guidance.
    warriors_programs = (
        await db_session.execute(
            text(
                "SELECT p.name, p.level FROM team_programs p "
                "JOIN organizations o ON o.id = p.organization_id "
                "WHERE o.slug = 'nba-warriors' ORDER BY p.level"
            )
        )
    ).all()
    assert {(row[0], row[1]) for row in warriors_programs} == {
        ("Golden State Warriors", "NBA"),
        ("Golden State Warriors Gold", "NBA-2"),
        ("Golden State Warriors Blue", "NBA-3"),
    }

    lakers_programs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM team_programs p "
                "JOIN organizations o ON o.id = p.organization_id "
                "WHERE o.slug = 'nba-lakers'"
            )
        )
    ).scalar_one()
    assert lakers_programs == 1  # Lakers field no sibling Summer League squad


@pytest.mark.asyncio
async def test_population_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """Running the population twice creates nothing the second time."""
    await _seed_multi_squad_franchises(db_session)
    primary_report = await run_primary_population(db_session)
    assert primary_report.failed == 0

    first = await run_population(db_session)
    assert first.team_programs_created == 8

    second = await run_population(db_session)

    assert second.planned == 8
    assert second.team_programs_created == 0
    assert second.team_programs_skipped == 8
    assert second.organization_missing == 0
    assert second.failed == 0

    program_count = await db_session.scalar(
        select(func.count()).select_from(TeamProgram)
    )
    # 5 primary programs (4 multi-squad + Lakers) + 8 siblings.
    assert program_count == 13


@pytest.mark.asyncio
async def test_dry_run_creates_nothing_and_matches_the_real_run_count(
    db_session: AsyncSession,
) -> None:
    """--dry-run reports the same pending count the real run then creates."""
    await _seed_multi_squad_franchises(db_session)
    primary_report = await run_primary_population(db_session)
    assert primary_report.failed == 0

    dry_report = await run_population(db_session, dry_run=True)
    assert dry_report.planned == 8
    assert dry_report.team_programs_created == 8

    program_count = await db_session.scalar(
        select(func.count()).select_from(TeamProgram)
    )
    assert program_count == 5  # only the primary programs T3 created

    real_report = await run_population(db_session)
    assert real_report.team_programs_created == dry_report.team_programs_created


@pytest.mark.asyncio
async def test_population_reports_organization_missing_when_t3_has_not_run(
    db_session: AsyncSession,
) -> None:
    """A franchise with no T3 organization yet is reported, not a crash.

    Seeds only the nba_teams rows -- deliberately skips T3's population --
    so every one of the 8 multi-squad targets is missing its parent
    organization.
    """
    await _seed_multi_squad_franchises(db_session)

    report = await run_population(db_session)

    assert report.planned == 8
    assert report.organization_missing == 8
    assert report.team_programs_created == 0
    assert report.failed == 0  # a missing prerequisite is not a poison target

    program_count = await db_session.scalar(
        select(func.count()).select_from(TeamProgram)
    )
    assert program_count == 0

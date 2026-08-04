"""Integration coverage for the player_affiliations.team_program_id backfill.

Seeds an ``nba_teams`` row, runs T3's org-model population against it (the
same script the backfill's join relies on), writes a ``PlayerAffiliation``
pointed at that franchise via ``nba_team_id``, then asserts the backfill
resolves it to the *same* franchise's ``team_program_id`` -- and that an
affiliation with a NULL ``nba_team_id`` is left untouched. Also covers
idempotency (a second run updates nothing) and the "T3 hasn't run yet" case
(no organizations/team_programs exist, so nothing is resolvable).

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, TeamProgram
from app.schemas.player_affiliation import AffiliationType, PlayerAffiliation
from app.services.player_affiliation import (
    NbaTeamRef,
    TeamProgramRef,
    resolve_affiliation_target,
)
from scripts.backfill_affiliation_team_program import run_backfill
from scripts.populate_org_model_from_nba_teams import run_population


async def _seed_team_and_program(
    db: AsyncSession, *, slug: str = "test-backfill-team"
) -> int:
    """Seed one nba_teams row and its T3 organization/team_program.

    Returns the team's id captured as a plain int -- ``run_population``
    issues its own commit/rollback internally, which expires ORM attributes,
    so the id must be read before that happens (see MissingGreenlet note in
    the ticket brief).
    """
    team = NbaTeam(name="Test Backfill Team", abbreviation="TBT", slug=slug)
    db.add(team)
    await db.commit()
    team_id = team.id
    assert team_id is not None

    report = await run_population(db)
    assert report.failed == 0
    return team_id


@pytest.mark.asyncio
async def test_backfill_resolves_existing_affiliation_to_the_same_franchise(
    db_session: AsyncSession,
) -> None:
    """A pre-existing SL affiliation resolves to the same franchise before/after.

    Before the backfill, resolution falls back to nba_team_id. After, it
    prefers the newly-set team_program_id -- but the franchise identity (via
    NbaTeam.id / TeamProgram.organization_id -> Organization.slug) must match
    both times.
    """
    team_id = await _seed_team_and_program(db_session)

    affiliation = PlayerAffiliation(
        player_id=None,
        nba_team_id=team_id,
        team_program_id=None,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        source="nba_summer_league_roster",
    )
    db_session.add(affiliation)
    await db_session.commit()
    affiliation_id = affiliation.id

    before = (
        await db_session.execute(
            select(PlayerAffiliation).where(PlayerAffiliation.id == affiliation_id)
        )
    ).scalar_one()
    before_resolved = resolve_affiliation_target(before)
    assert before_resolved == NbaTeamRef(nba_team_id=team_id)

    report = await run_backfill(db_session)
    assert report.updated == 1
    assert report.unresolvable == 0

    after = (
        await db_session.execute(
            select(PlayerAffiliation).where(PlayerAffiliation.id == affiliation_id)
        )
    ).scalar_one()
    assert after.nba_team_id == team_id
    after_resolved = resolve_affiliation_target(after)
    assert isinstance(after_resolved, TeamProgramRef)

    # Same franchise: the resolved program belongs to the organization T3
    # created for this exact nba_teams row.
    program = (
        await db_session.execute(
            select(TeamProgram).where(TeamProgram.id == after_resolved.team_program_id)
        )
    ).scalar_one()
    organization = (
        await db_session.execute(
            select(Organization).where(Organization.id == program.organization_id)
        )
    ).scalar_one()
    assert organization.slug == "nba-test-backfill-team"


@pytest.mark.asyncio
async def test_backfill_leaves_null_nba_team_id_affiliations_untouched(
    db_session: AsyncSession,
) -> None:
    """An affiliation with no nba_team_id never gets an invented target."""
    affiliation = PlayerAffiliation(
        player_id=None,
        nba_team_id=None,
        team_program_id=None,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        source="nba_summer_league_box_score",
    )
    db_session.add(affiliation)
    await db_session.commit()
    affiliation_id = affiliation.id

    report = await run_backfill(db_session)
    assert report.updated == 0
    assert report.left_null >= 1

    after = (
        await db_session.execute(
            select(PlayerAffiliation).where(PlayerAffiliation.id == affiliation_id)
        )
    ).scalar_one()
    assert after.nba_team_id is None
    assert after.team_program_id is None


@pytest.mark.asyncio
async def test_backfill_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """Running the backfill twice updates rows only the first time."""
    team_id = await _seed_team_and_program(db_session, slug="test-backfill-idem")

    db_session.add(
        PlayerAffiliation(
            player_id=None,
            nba_team_id=team_id,
            team_program_id=None,
            affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
            source="nba_summer_league_roster",
        )
    )
    await db_session.commit()

    first = await run_backfill(db_session)
    assert first.updated == 1

    second = await run_backfill(db_session)
    assert second.updated == 0
    assert second.eligible == 0


@pytest.mark.asyncio
async def test_dry_run_reports_counts_without_writing(db_session: AsyncSession) -> None:
    """--dry-run reports the same eligible count the real run then updates."""
    team_id = await _seed_team_and_program(db_session, slug="test-backfill-dry")

    db_session.add(
        PlayerAffiliation(
            player_id=None,
            nba_team_id=team_id,
            team_program_id=None,
            affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
            source="nba_summer_league_roster",
        )
    )
    await db_session.commit()

    dry_report = await run_backfill(db_session, dry_run=True)
    assert dry_report.eligible == 1
    assert dry_report.updated == 0

    unchanged = (
        await db_session.execute(
            select(PlayerAffiliation).where(
                PlayerAffiliation.nba_team_id == team_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert unchanged.team_program_id is None

    real_report = await run_backfill(db_session)
    assert real_report.updated == dry_report.eligible


@pytest.mark.asyncio
async def test_backfill_reports_unresolvable_when_org_model_not_populated(
    db_session: AsyncSession,
) -> None:
    """A franchise with no T3 organization/team_program yet is unresolvable, not invented."""
    team = NbaTeam(
        name="Unpopulated Team", abbreviation="UPT", slug="test-unpopulated-team"
    )
    db_session.add(team)
    await db_session.commit()
    team_id = team.id

    db_session.add(
        PlayerAffiliation(
            player_id=None,
            nba_team_id=team_id,
            team_program_id=None,
            affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
            source="nba_summer_league_roster",
        )
    )
    await db_session.commit()

    report = await run_backfill(db_session)
    assert report.updated == 0
    assert report.unresolvable == 1

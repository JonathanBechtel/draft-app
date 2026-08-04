"""Integration coverage for the summer_league_team_entries.team_program_id backfill.

Seeds an ``nba_teams`` row, runs T3's org-model population against it (the
same script the backfill's join relies on), writes a ``SummerLeagueTeamEntry``
pointed at that franchise via ``nba_team_id``, then asserts the backfill
resolves it to the *same* franchise's ``team_program_id`` -- and that an entry
with a NULL ``nba_team_id`` is left untouched. Also covers idempotency (a
second run updates nothing), the "T3 hasn't run yet" case (nothing
resolvable), and the spoke-#2 shape: a team entry created with
``team_program_id`` set and ``nba_team_id`` NULL.

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, OrgKind, TeamProgram
from app.schemas.summer_league import SummerLeagueCompetition, SummerLeagueTeamEntry
from app.services.player_affiliation import (
    NbaTeamRef,
    TeamProgramRef,
    resolve_team_target,
)
from scripts.backfill_sl_team_entry_team_program import run_backfill
from scripts.populate_org_model_from_nba_teams import run_population


async def _seed_team_and_program(
    db: AsyncSession, *, slug: str = "test-sl-backfill-team"
) -> int:
    """Seed one nba_teams row and its T3 organization/team_program.

    Returns the team's id captured as a plain int -- ``run_population``
    issues its own commit/rollback internally, which expires ORM attributes,
    so the id must be read before that happens.
    """
    team = NbaTeam(name="Test SL Backfill Team", abbreviation="TSBT", slug=slug)
    db.add(team)
    await db.commit()
    team_id = team.id
    assert team_id is not None

    report = await run_population(db)
    assert report.failed == 0
    return team_id


async def _seed_competition(db: AsyncSession) -> int:
    """Seed one minimal Summer League competition to satisfy the FK."""
    comp = SummerLeagueCompetition(
        year=2026,
        league_id="test-sl-backfill-league",
        venue_slug="test-sl-backfill-venue",
        display_name="Test SL Backfill Competition",
    )
    db.add(comp)
    await db.commit()
    comp_id = comp.id
    assert comp_id is not None
    return comp_id


@pytest.mark.asyncio
async def test_backfill_resolves_existing_team_entry_to_the_same_franchise(
    db_session: AsyncSession,
) -> None:
    """A pre-existing SL team entry resolves to the same franchise before/after.

    Before the backfill, resolution falls back to nba_team_id. After, it
    prefers the newly-set team_program_id -- but the franchise identity (via
    NbaTeam.id / TeamProgram.organization_id -> Organization.slug) must match
    both times.
    """
    team_id = await _seed_team_and_program(db_session)
    comp_id = await _seed_competition(db_session)

    entry = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=team_id,
        team_program_id=None,
        nba_stats_team_id="test-sl-backfill-source-team",
        raw_team_name="Test Backfill Team",
        team_slug="test-backfill-team",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    before = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(SummerLeagueTeamEntry.id == entry_id)
        )
    ).scalar_one()
    before_resolved = resolve_team_target(before)
    assert before_resolved == NbaTeamRef(nba_team_id=team_id)

    report = await run_backfill(db_session)
    assert report.updated == 1
    assert report.unresolvable == 0

    after = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(SummerLeagueTeamEntry.id == entry_id)
        )
    ).scalar_one()
    assert after.nba_team_id == team_id
    after_resolved = resolve_team_target(after)
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
    assert organization.slug == "nba-test-sl-backfill-team"


@pytest.mark.asyncio
async def test_backfill_leaves_null_nba_team_id_entries_untouched(
    db_session: AsyncSession,
) -> None:
    """A team entry with no nba_team_id never gets an invented target."""
    comp_id = await _seed_competition(db_session)

    entry = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=None,
        team_program_id=None,
        nba_stats_team_id="test-sl-backfill-unresolved-team",
        raw_team_name="Unresolved Team",
        team_slug="unresolved-team",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    report = await run_backfill(db_session)
    assert report.updated == 0
    assert report.left_null >= 1

    after = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(SummerLeagueTeamEntry.id == entry_id)
        )
    ).scalar_one()
    assert after.nba_team_id is None
    assert after.team_program_id is None


@pytest.mark.asyncio
async def test_backfill_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """Running the backfill twice updates rows only the first time."""
    team_id = await _seed_team_and_program(db_session, slug="test-sl-backfill-idem")
    comp_id = await _seed_competition(db_session)

    db_session.add(
        SummerLeagueTeamEntry(
            competition_id=comp_id,
            nba_team_id=team_id,
            team_program_id=None,
            nba_stats_team_id="test-sl-backfill-idem-team",
            raw_team_name="Idempotent Team",
            team_slug="idempotent-team",
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
    team_id = await _seed_team_and_program(db_session, slug="test-sl-backfill-dry")
    comp_id = await _seed_competition(db_session)

    db_session.add(
        SummerLeagueTeamEntry(
            competition_id=comp_id,
            nba_team_id=team_id,
            team_program_id=None,
            nba_stats_team_id="test-sl-backfill-dry-team",
            raw_team_name="Dry Run Team",
            team_slug="dry-run-team",
        )
    )
    await db_session.commit()

    dry_report = await run_backfill(db_session, dry_run=True)
    assert dry_report.eligible == 1
    assert dry_report.updated == 0

    unchanged = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(
                SummerLeagueTeamEntry.nba_team_id == team_id  # type: ignore[arg-type]
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
    comp_id = await _seed_competition(db_session)
    team = NbaTeam(
        name="Unpopulated SL Team", abbreviation="UST", slug="test-sl-unpopulated-team"
    )
    db_session.add(team)
    await db_session.commit()
    team_id = team.id

    db_session.add(
        SummerLeagueTeamEntry(
            competition_id=comp_id,
            nba_team_id=team_id,
            team_program_id=None,
            nba_stats_team_id="test-sl-unpopulated-team-source",
            raw_team_name="Unpopulated Team",
            team_slug="unpopulated-team",
        )
    )
    await db_session.commit()

    report = await run_backfill(db_session)
    assert report.updated == 0
    assert report.unresolvable == 1


@pytest.mark.asyncio
async def test_team_entry_can_be_created_with_team_program_id_and_null_nba_team_id(
    db_session: AsyncSession,
) -> None:
    """The spoke-#2 shape: a team entry can target a program with no NBA team.

    Mechanical proof the column supports a future non-NBA-sourced team entry
    without requiring an nba_team_id, mirroring T4's
    test_affiliation_non_nba_source.py exit-criterion check.
    """
    comp_id = await _seed_competition(db_session)

    organization = Organization(
        org_kind=OrgKind.FEDERATION,
        name="Test SL Federation",
        slug="test-sl-federation",
    )
    db_session.add(organization)
    await db_session.flush()
    assert organization.id is not None

    program = TeamProgram(
        organization_id=organization.id,
        name="Test SL Federation U19 Squad",
        slug="test-sl-federation-u19",
        level="U19",
    )
    db_session.add(program)
    await db_session.flush()
    program_id = program.id
    assert program_id is not None

    entry = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=None,
        team_program_id=program_id,
        nba_stats_team_id="test-sl-federation-source-team",
        raw_team_name="Test Federation Squad",
        team_slug="test-federation-squad",
    )
    db_session.add(entry)
    await db_session.commit()

    persisted = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(
                SummerLeagueTeamEntry.nba_stats_team_id
                == "test-sl-federation-source-team"
            )
        )
    ).scalar_one()

    assert persisted.team_program_id == program_id
    assert persisted.nba_team_id is None

    resolved = resolve_team_target(persisted)
    assert resolved == TeamProgramRef(team_program_id=program_id)


# --------------------------------------------------------------------------- #
# Schema shape (from-scratch-DB path)
# --------------------------------------------------------------------------- #
#
# The local docker Postgres schema is bootstrapped fresh via
# SQLModel.metadata.create_all against the current model classes for every
# test session (see tests/integration/conftest.py) -- this *is* the
# from-scratch-DB path the f8855e75c831 migration's guards must no-op
# against. These checks confirm create_all produced exactly the additive
# shape the ticket calls for, mirroring
# tests/integration/test_player_affiliation_schema.py (T4, #783).


@pytest.mark.asyncio
async def test_team_program_id_column_and_index_exist(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """The additive column and its index are present on the fresh schema."""
    async with async_engine.connect() as conn:
        await conn.execute(text(f'SET search_path TO "{test_schema}"'))
        columns = set(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'summer_league_team_entries'"
                    ),
                    {"schema": test_schema},
                )
            ).scalars()
        )
        indexes = set(
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema "
                        "AND tablename = 'summer_league_team_entries'"
                    ),
                    {"schema": test_schema},
                )
            ).scalars()
        )

    # New, additive column plus the two it stands beside, untouched.
    assert {"team_program_id", "nba_team_id", "nba_stats_team_id"} <= columns
    assert "ix_summer_league_team_entries_team_program_id" in indexes


@pytest.mark.asyncio
async def test_existing_uq_and_nba_stats_team_id_are_untouched(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """The pre-existing unique constraint and provider id column are unchanged."""
    async with async_engine.connect() as conn:
        await conn.execute(text(f'SET search_path TO "{test_schema}"'))
        constraints = (
            await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'summer_league_team_entries'::regclass "
                    "AND contype = 'u'"
                )
            )
        ).scalars()
        nba_stats_column = (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = :schema "
                    "AND table_name = 'summer_league_team_entries' "
                    "AND column_name = 'nba_stats_team_id'"
                ),
                {"schema": test_schema},
            )
        ).scalar_one()

    assert set(constraints) == {"uq_summer_league_team_entries_competition_source_team"}
    assert nba_stats_column == "NO"

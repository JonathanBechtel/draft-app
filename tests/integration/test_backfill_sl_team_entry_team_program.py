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
from app.schemas.summer_league import SummerLeagueEdition, SummerLeagueTeamEntry
from app.services.backbone.team_program_resolution import AmbiguousTeamProgramError
from app.services.player_affiliation import (
    NbaTeamRef,
    TeamProgramRef,
    resolve_team_target,
)
from scripts.backfill_sl_team_entry_team_program import (
    format_report_lines,
    run_backfill,
)
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
    comp = SummerLeagueEdition(
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

    # expunge_all() first: the fixture sets expire_on_commit=False, so an ORM
    # re-read can be answered from the identity map. It happens to see the real
    # write today only because the script updates via ORM-enabled update(Entity),
    # which syncs the identity map -- switch that to text("UPDATE ...") or
    # synchronize_session=False and this assertion goes silently inert.
    db_session.expunge_all()
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
    assert report.left_null == 1

    db_session.expunge_all()
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

    # #799 rewires the map builder but must not change the operator report
    # shape for the single-program case -- other tickets in this project
    # cite these exact eligible/updated/unresolvable/left_null counts as
    # evidence.
    assert format_report_lines(dry_report, dry_run=True) == [
        "summer_league_team_entries team_program_id backfill (dry-run): "
        f"eligible={dry_report.eligible} updated={dry_report.updated} "
        f"unresolvable={dry_report.unresolvable} left_null={dry_report.left_null}"
    ]

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
async def test_backfill_refuses_when_an_organization_owns_two_team_programs(
    db_session: AsyncSession,
) -> None:
    """A franchise organization with two primary-level team_programs must not
    silently pick one.

    Regression for #799: both backfill scripts used to key their franchise
    map on ``organization_id`` via a plain dict comprehension with no
    ``ORDER BY`` -- when an organization owned more than one
    ``team_programs`` row, whichever row Postgres happened to return last
    silently won, and not even stably across runs. This seeds a second
    program under the same organization T3 already created and asserts the
    rewired script refuses (raises ``AmbiguousTeamProgramError``, via the
    #796 backbone resolver) instead of guessing -- and that no row got an
    arbitrary target as a side effect of the attempt.

    This test fails against the pre-#799 script (confirmed by running it
    against the unmodified ``_franchise_team_program_map`` dict-comprehension
    implementation before this ticket's rewire): that code raises nothing
    and simply returns one of the two programs.

    Level (#810): the second program here shares
    ``PRIMARY_TEAM_PROGRAM_LEVEL`` ("NBA") with the one T3 created, which is
    what makes this a genuine same-level duplicate -- and still ambiguous --
    rather than a legitimate multi-squad sibling (a *different* level, e.g.
    ``NBA-2``), which the #810 rescoped query now correctly excludes from
    this guard.
    """
    team_id = await _seed_team_and_program(db_session, slug="test-sl-backfill-ambiguous")
    comp_id = await _seed_competition(db_session)

    # Raw SQL, not the ORM, per this repo's anti-vacuity guidance: SQLModel
    # silently discards unknown constructor kwargs, so at least one
    # assertion here reads real columns directly rather than trusting a
    # constructed object.
    organization_id = (
        await db_session.execute(
            text("SELECT id FROM organizations WHERE slug = :slug"),
            {"slug": "nba-test-sl-backfill-ambiguous"},
        )
    ).scalar_one()

    db_session.add(
        TeamProgram(
            organization_id=organization_id,
            name="Test SL Backfill Ambiguous Duplicate Primary",
            slug="test-sl-backfill-ambiguous-duplicate-primary",
            level="NBA",
        )
    )
    await db_session.commit()
    # db_session runs with expire_on_commit=False, so a stale ORM identity
    # map would otherwise mask the row this test just added.
    db_session.expunge_all()

    db_session.add(
        SummerLeagueTeamEntry(
            competition_id=comp_id,
            nba_team_id=team_id,
            team_program_id=None,
            nba_stats_team_id="test-sl-backfill-ambiguous-source-team",
            raw_team_name="Ambiguous Team",
            team_slug="ambiguous-team",
        )
    )
    await db_session.commit()
    db_session.expunge_all()

    with pytest.raises(AmbiguousTeamProgramError):
        await run_backfill(db_session)

    # No arbitrary write happened as a side effect of the attempt -- read
    # the real column with raw SQL rather than trusting a possibly-expired
    # ORM object.
    unchanged_team_program_id = (
        await db_session.execute(
            text(
                "SELECT team_program_id FROM summer_league_team_entries "
                "WHERE nba_team_id = :team_id"
            ),
            {"team_id": team_id},
        )
    ).scalar_one()
    assert unchanged_team_program_id is None


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
# Strategy 2: nba_stats_team_id resolution (#808)
# --------------------------------------------------------------------------- #
#
# #784's one-off backfill (and #796's ingest-time write) only ever populated
# nba_team_id; nothing has ever backfilled it for historical rows from the
# provider id. Measuring dev after #807 found 103 of 622 team entries with
# nba_team_id NULL, and 82 of those carry a real NBA franchise
# nba_stats_team_id that was simply never written. These tests seed a real
# NBA_STATS_TEAM_ID_TO_ABBREVIATION entry (e.g. "1610612737" / Atlanta Hawks)
# since the resolver keys off that hardcoded map, not any test-only
# abbreviation.


@pytest.mark.asyncio
async def test_backfill_resolves_null_nba_team_id_entry_from_stats_team_id(
    db_session: AsyncSession,
) -> None:
    """#808: nba_team_id NULL + a real NBA nba_stats_team_id resolves BOTH targets.

    This is the gap #808 exists to close. Must fail against the pre-#808
    script (confirmed by reverting scripts/backfill_sl_team_entry_team_program.py
    to its single-strategy form and running this test: it errors with
    AttributeError since BackfillReport had no ``stats_id`` field, and even a
    version of this test written against only the old fields would still show
    the entry's nba_team_id/team_program_id staying NULL).
    """
    team = NbaTeam(name="Atlanta Hawks", abbreviation="ATL", slug="test-sl-hawks-808")
    db_session.add(team)
    await db_session.commit()
    team_id = team.id
    assert team_id is not None

    pop_report = await run_population(db_session)
    assert pop_report.failed == 0

    comp_id = await _seed_competition(db_session)
    entry = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=None,
        team_program_id=None,
        nba_stats_team_id="1610612737",
        raw_team_name="Atlanta Hawks",
        team_slug="test-sl-hawks-808-entry",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    backfill_report = await run_backfill(db_session)
    assert backfill_report.stats_id.eligible == 1
    assert backfill_report.stats_id.updated == 1
    assert backfill_report.stats_id.unresolvable == 0
    assert backfill_report.stats_id.uncovered == 0

    # Raw SQL per this repo's anti-vacuity guidance: confirms the real
    # columns, not a possibly-stale ORM identity map (db_session runs with
    # expire_on_commit=False).
    db_session.expunge_all()
    nba_team_id, team_program_id = (
        await db_session.execute(
            text(
                "SELECT nba_team_id, team_program_id FROM summer_league_team_entries "
                "WHERE id = :id"
            ),
            {"id": entry_id},
        )
    ).one()
    assert nba_team_id == team_id
    assert team_program_id is not None

    program = (
        await db_session.execute(
            select(TeamProgram).where(TeamProgram.id == team_program_id)
        )
    ).scalar_one()
    organization = (
        await db_session.execute(
            select(Organization).where(Organization.id == program.organization_id)
        )
    ).scalar_one()
    assert organization.slug == "nba-test-sl-hawks-808"


@pytest.mark.asyncio
async def test_backfill_leaves_non_nba_stats_id_null_and_reports_it(
    db_session: AsyncSession,
) -> None:
    """A genuinely non-NBA stats id (Team China, "45") stays NULL on both
    columns and surfaces in the uncovered report -- never silently skipped.
    """
    comp_id = await _seed_competition(db_session)
    entry = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=None,
        team_program_id=None,
        nba_stats_team_id="45",
        raw_team_name="Team China Basketball",
        team_slug="test-sl-team-china-808",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    report = await run_backfill(db_session)
    assert report.stats_id.eligible == 0
    assert report.stats_id.updated == 0
    assert report.stats_id.uncovered == 1
    assert report.stats_id.uncovered_stats_ids == ["45"]

    lines = format_report_lines(report)
    assert any("uncovered=1" in line and "uncovered_ids=45" in line for line in lines)

    db_session.expunge_all()
    after = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(SummerLeagueTeamEntry.id == entry_id)
        )
    ).scalar_one()
    assert after.nba_team_id is None
    assert after.team_program_id is None


@pytest.mark.asyncio
async def test_backfill_does_not_repoint_an_already_targeted_entry(
    db_session: AsyncSession,
) -> None:
    """An entry that already carries team_program_id (e.g. a non-NBA org's
    entry created via #796's ingest-time write) is left untouched by
    strategy 2, even though its nba_stats_team_id happens to be covered by
    the map -- D3's "never repoint" rule.
    """
    comp_id = await _seed_competition(db_session)

    organization = Organization(
        org_kind=OrgKind.FEDERATION,
        name="Test SL 808 Federation",
        slug="test-sl-808-federation",
    )
    db_session.add(organization)
    await db_session.flush()
    assert organization.id is not None

    program = TeamProgram(
        organization_id=organization.id,
        name="Test SL 808 Federation Squad",
        slug="test-sl-808-federation-squad",
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
        # Boston Celtics -- a covered id, but must not be applied since
        # team_program_id is already set.
        nba_stats_team_id="1610612738",
        raw_team_name="Already Targeted Entry",
        team_slug="test-sl-808-already-targeted",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    report = await run_backfill(db_session)
    assert report.stats_id.eligible == 0
    assert report.stats_id.updated == 0

    db_session.expunge_all()
    unchanged_program_id, unchanged_nba_team_id = (
        await db_session.execute(
            text(
                "SELECT team_program_id, nba_team_id FROM summer_league_team_entries "
                "WHERE id = :id"
            ),
            {"id": entry_id},
        )
    ).one()
    assert unchanged_program_id == program_id
    assert unchanged_nba_team_id is None


@pytest.mark.asyncio
async def test_backfill_stats_id_strategy_is_idempotent_on_rerun(
    db_session: AsyncSession,
) -> None:
    """A second run of the nba_stats_team_id strategy reports zero eligible."""
    team = NbaTeam(name="Boston Celtics", abbreviation="BOS", slug="test-sl-celtics-808")
    db_session.add(team)
    await db_session.commit()

    pop_report = await run_population(db_session)
    assert pop_report.failed == 0

    comp_id = await _seed_competition(db_session)
    db_session.add(
        SummerLeagueTeamEntry(
            competition_id=comp_id,
            nba_team_id=None,
            team_program_id=None,
            nba_stats_team_id="1610612738",
            raw_team_name="Boston Celtics",
            team_slug="test-sl-celtics-808-entry",
        )
    )
    await db_session.commit()

    first = await run_backfill(db_session)
    assert first.stats_id.updated == 1

    second = await run_backfill(db_session)
    assert second.stats_id.updated == 0
    assert second.stats_id.eligible == 0


@pytest.mark.asyncio
async def test_backfill_does_not_crash_on_blank_raw_team_name(
    db_session: AsyncSession,
) -> None:
    """An entry with an empty raw_team_name (the real dev shape for the
    D-League Select entry, stats id "1612709916") must not crash the
    resolver or the report -- neither touches raw_team_name.
    """
    comp_id = await _seed_competition(db_session)
    entry = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=None,
        team_program_id=None,
        nba_stats_team_id="1612709916",
        raw_team_name="",
        team_slug="test-sl-808-blank-name",
    )
    db_session.add(entry)
    await db_session.commit()
    entry_id = entry.id

    report = await run_backfill(db_session)
    assert report.stats_id.uncovered == 1
    assert "1612709916" in report.stats_id.uncovered_stats_ids

    db_session.expunge_all()
    after = (
        await db_session.execute(
            select(SummerLeagueTeamEntry).where(SummerLeagueTeamEntry.id == entry_id)
        )
    ).scalar_one()
    assert after.raw_team_name == ""
    assert after.nba_team_id is None
    assert after.team_program_id is None


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

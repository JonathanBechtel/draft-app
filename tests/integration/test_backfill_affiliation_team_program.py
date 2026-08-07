"""Integration coverage for the player_affiliations.team_program_id backfill.

Seeds an ``nba_teams`` row, runs T3's org-model population against it (the
same script the backfill's join relies on), writes a ``PlayerAffiliation``
pointed at that franchise via ``nba_team_id``, then asserts the backfill
resolves it to the *same* franchise's ``team_program_id`` -- and that an
affiliation with a NULL ``nba_team_id`` is left untouched. Also covers
idempotency (a second run updates nothing) and the "T3 hasn't run yet" case
(no organizations/team_programs exist, so nothing is resolvable).

Ticket #807 adds coverage for the second (participation-bridge) resolution
strategy: historical affiliations have ``nba_team_id`` NULL too, so the
franchise join above has nothing to derive from for them. Those rows are only
reachable through ``summer_league_participation.affiliation_id ->
summer_league_team_entries.team_program_id``. See
``test_bridge_resolves_historical_affiliation_with_null_nba_team_id`` -- this
is the test that must fail against the pre-#807 script.

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, TeamProgram
from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.backbone.team_program_resolution import AmbiguousTeamProgramError
from app.services.player_affiliation import (
    NbaTeamRef,
    TeamProgramRef,
    resolve_affiliation_target,
)
from scripts._franchise_team_program_map import franchise_nba_team_id_to_team_program_id
from scripts.backfill_affiliation_team_program import format_report_lines, run_backfill
from scripts.populate_org_model_from_nba_teams import run_population


async def _seed_team_and_program(
    db: AsyncSession, *, slug: str = "test-backfill-team", abbreviation: str = "TBT"
) -> int:
    """Seed one nba_teams row and its T3 organization/team_program.

    Returns the team's id captured as a plain int -- ``run_population``
    issues its own commit/rollback internally, which expires ORM attributes,
    so the id must be read before that happens (see MissingGreenlet note in
    the ticket brief).

    ``abbreviation`` must be unique per call within a test (``nba_teams`` has
    a unique index on it) -- callers seeding more than one franchise in the
    same test must pass distinct values.
    """
    team = NbaTeam(name="Test Backfill Team", abbreviation=abbreviation, slug=slug)
    db.add(team)
    await db.commit()
    team_id = team.id
    assert team_id is not None

    report = await run_population(db)
    assert report.failed == 0
    return team_id


async def _team_program_id_for(db: AsyncSession, nba_team_id: int) -> int:
    """Look up the real ``team_programs.id`` T3 created for ``nba_team_id``.

    ``PlayerAffiliation.team_program_id`` carries a hard FK to
    ``team_programs.id``, so bridge-fixture tests need a genuine row here
    (unlike ``summer_league_team_entries.team_program_id``, a soft reference
    that would tolerate an arbitrary int).
    """
    franchise_map = await franchise_nba_team_id_to_team_program_id(db)
    return franchise_map[nba_team_id]


async def _seed_edition(db: AsyncSession, *, league_id: str = "15") -> int:
    """Seed one minimal SummerLeagueEdition row and return its id."""
    edition = SummerLeagueEdition(
        year=2026,
        league_id=league_id,
        venue_slug="las_vegas",
        display_name="Test Bridge Edition",
    )
    db.add(edition)
    await db.commit()
    edition_id = edition.id
    assert edition_id is not None
    return edition_id


async def _seed_team_entry(
    db: AsyncSession,
    *,
    competition_id: int,
    nba_stats_team_id: str,
    nba_team_id: int | None = None,
    team_program_id: int | None = None,
) -> int:
    """Seed one SummerLeagueTeamEntry row and return its id."""
    entry = SummerLeagueTeamEntry(
        competition_id=competition_id,
        nba_team_id=nba_team_id,
        team_program_id=team_program_id,
        nba_stats_team_id=nba_stats_team_id,
        raw_team_name="Test Bridge Team",
        team_slug=f"test-bridge-team-{nba_stats_team_id}",
    )
    db.add(entry)
    await db.commit()
    entry_id = entry.id
    assert entry_id is not None
    return entry_id


async def _seed_source_player(db: AsyncSession, *, nba_stats_person_id: str) -> int:
    """Seed one SummerLeagueSourceRecord row and return its id."""
    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id=nba_stats_person_id,
        raw_player_name=f"Test Player {nba_stats_person_id}",
        normalized_name=f"test player {nba_stats_person_id}",
    )
    db.add(source_player)
    await db.commit()
    source_player_id = source_player.id
    assert source_player_id is not None
    return source_player_id


async def _seed_affiliation(
    db: AsyncSession,
    *,
    nba_team_id: int | None = None,
    team_program_id: int | None = None,
    source: str = "nba_summer_league_roster",
    status: AffiliationStatus = AffiliationStatus.ANNOUNCED,
    supersedes_id: int | None = None,
    superseded_at: object = None,
) -> int:
    """Seed one PlayerAffiliation row and return its id."""
    affiliation = PlayerAffiliation(
        player_id=None,
        nba_team_id=nba_team_id,
        team_program_id=team_program_id,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=status,
        source=source,
        supersedes_id=supersedes_id,
        superseded_at=superseded_at,  # type: ignore[arg-type]
    )
    db.add(affiliation)
    await db.commit()
    affiliation_id = affiliation.id
    assert affiliation_id is not None
    return affiliation_id


async def _seed_participation(
    db: AsyncSession,
    *,
    competition_id: int,
    team_entry_id: int,
    source_player_id: int,
    affiliation_id: int,
    stint_no: int = 1,
) -> int:
    """Seed one SummerLeagueParticipation row and return its id."""
    participation = SummerLeagueParticipation(
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        player_id=None,
        affiliation_id=affiliation_id,
        stint_no=stint_no,
    )
    db.add(participation)
    await db.commit()
    participation_id = participation.id
    assert participation_id is not None
    return participation_id


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

    # expunge_all() first: the fixture sets expire_on_commit=False, so an ORM
    # re-read can be answered from the identity map. It happens to see the real
    # write today only because the script updates via ORM-enabled update(Entity),
    # which syncs the identity map -- switch that to text("UPDATE ...") or
    # synchronize_session=False and this assertion goes silently inert.
    db_session.expunge_all()
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
    assert report.left_null == 1

    db_session.expunge_all()
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

    # #799 rewires the map builder but must not change the operator report
    # shape for the single-program case -- other tickets in this project
    # cite these exact eligible/updated/unresolvable/left_null counts as
    # evidence.
    assert format_report_lines(dry_report, dry_run=True) == [
        "player_affiliations team_program_id backfill (dry-run): "
        f"eligible={dry_report.eligible} updated={dry_report.updated} "
        f"unresolvable={dry_report.unresolvable} left_null={dry_report.left_null}"
    ]

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
    team_id = await _seed_team_and_program(db_session, slug="test-backfill-ambiguous")

    # Raw SQL, not the ORM, per this repo's anti-vacuity guidance: SQLModel
    # silently discards unknown constructor kwargs, so at least one
    # assertion here reads real columns directly rather than trusting a
    # constructed object.
    organization_id = (
        await db_session.execute(
            text("SELECT id FROM organizations WHERE slug = :slug"),
            {"slug": "nba-test-backfill-ambiguous"},
        )
    ).scalar_one()

    db_session.add(
        TeamProgram(
            organization_id=organization_id,
            name="Test Backfill Ambiguous Duplicate Primary",
            slug="test-backfill-ambiguous-duplicate-primary",
            level="NBA",
        )
    )
    await db_session.commit()
    # db_session runs with expire_on_commit=False, so a stale ORM identity
    # map would otherwise mask the row this test just added.
    db_session.expunge_all()

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
    db_session.expunge_all()

    with pytest.raises(AmbiguousTeamProgramError):
        await run_backfill(db_session)

    # No arbitrary write happened as a side effect of the attempt -- read
    # the real column with raw SQL rather than trusting a possibly-expired
    # ORM object.
    unchanged_team_program_id = (
        await db_session.execute(
            text(
                "SELECT team_program_id FROM player_affiliations "
                "WHERE nba_team_id = :team_id"
            ),
            {"team_id": team_id},
        )
    ).scalar_one()
    assert unchanged_team_program_id is None


# ---------------------------------------------------------------------------
# Strategy 2: participation-bridge resolution (#807)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_resolves_historical_affiliation_with_null_nba_team_id(
    db_session: AsyncSession,
) -> None:
    """A historical affiliation with nba_team_id NULL resolves via the bridge.

    Every real historical row this ticket exists for has BOTH targets NULL --
    the franchise join (strategy 1) has no ``nba_team_id`` to derive from, so
    it can never reach these rows. The only path in is
    ``summer_league_participation.affiliation_id ->
    summer_league_team_entries.team_program_id``: the team entry carries a
    resolved target (as #784's backfill or #796's ingest-time write would
    produce) even though the affiliation itself predates that resolution.

    This is the test that must fail against the pre-#807 script -- confirmed
    by running it against the unmodified script (only the ``nba_team_id``
    franchise join, no bridge) before implementing the fix: the affiliation's
    ``team_program_id`` stayed NULL and ``report.updated`` was 0.
    """
    team_id = await _seed_team_and_program(db_session, slug="test-bridge-basic")
    team_program_id = await _team_program_id_for(db_session, team_id)

    edition_id = await _seed_edition(db_session)
    team_entry_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9001",
        nba_team_id=team_id,
        team_program_id=team_program_id,
    )
    source_player_id = await _seed_source_player(db_session, nba_stats_person_id="BR1")
    affiliation_id = await _seed_affiliation(db_session)  # both targets NULL
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        affiliation_id=affiliation_id,
    )

    report = await run_backfill(db_session)

    # Strategy 1 (nba_team_id join) sees nothing -- the affiliation's
    # nba_team_id was NULL, exactly the defect this ticket closes.
    assert report.eligible == 0
    assert report.updated == 0

    assert report.bridge.updated == 1
    assert report.bridge.already_set_disagreement == 0

    db_session.expunge_all()
    resolved = (
        await db_session.execute(
            text("SELECT team_program_id FROM player_affiliations WHERE id = :id"),
            {"id": affiliation_id},
        )
    ).scalar_one()
    assert resolved == team_program_id


@pytest.mark.asyncio
async def test_bridge_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """A second bridge run reports zero eligible and updates nothing."""
    team_id = await _seed_team_and_program(db_session, slug="test-bridge-idem")
    team_program_id = await _team_program_id_for(db_session, team_id)

    edition_id = await _seed_edition(db_session)
    team_entry_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9002",
        nba_team_id=team_id,
        team_program_id=team_program_id,
    )
    source_player_id = await _seed_source_player(db_session, nba_stats_person_id="BR2")
    affiliation_id = await _seed_affiliation(db_session)
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        affiliation_id=affiliation_id,
    )

    first = await run_backfill(db_session)
    assert first.bridge.updated == 1

    second = await run_backfill(db_session)
    assert second.bridge.eligible == 0
    assert second.bridge.updated == 0


@pytest.mark.asyncio
async def test_bridge_backfills_the_whole_supersede_chain(
    db_session: AsyncSession,
) -> None:
    """Superseded rows in the chain get the same target as the latest row.

    Decision (documented in the script's docstring): walk ``supersedes_id``
    back up the full chain and backfill every row in it, not just the latest
    one ``participation.affiliation_id`` points at. A superseded row is a
    historical assertion in its own right; leaving it NULL while its
    successor carries a target would make the chain internally inconsistent
    for any reader comparing assertions across it.

    Also proves the backfill is not a blanket sweep: an unrelated
    affiliation with no participation link at all is asserted untouched.
    """
    team_id = await _seed_team_and_program(db_session, slug="test-bridge-chain")
    team_program_id = await _team_program_id_for(db_session, team_id)

    edition_id = await _seed_edition(db_session)
    team_entry_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9003",
        nba_team_id=team_id,
        team_program_id=team_program_id,
    )
    source_player_id = await _seed_source_player(db_session, nba_stats_person_id="BR3")

    # Build a two-row supersede chain by hand: root (ANNOUNCED) -> cut
    # (CUT, supersedes_id -> root). participation.affiliation_id points only
    # at the latest (cut) row, mirroring the real roster-ingest supersede
    # path.
    root_id = await _seed_affiliation(db_session, status=AffiliationStatus.ANNOUNCED)
    cut_id = await _seed_affiliation(
        db_session,
        status=AffiliationStatus.CUT,
        supersedes_id=root_id,
    )
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        affiliation_id=cut_id,
    )

    # An unrelated affiliation, no participation link -- must stay NULL.
    control_id = await _seed_affiliation(db_session)

    report = await run_backfill(db_session)
    assert report.bridge.updated == 2  # both root and cut in the chain

    db_session.expunge_all()
    rows = (
        await db_session.execute(
            text(
                "SELECT id, team_program_id FROM player_affiliations "
                "WHERE id IN (:root_id, :cut_id, :control_id)"
            ),
            {"root_id": root_id, "cut_id": cut_id, "control_id": control_id},
        )
    ).all()
    by_id = {row[0]: row[1] for row in rows}
    assert by_id[root_id] == team_program_id
    assert by_id[cut_id] == team_program_id
    assert by_id[control_id] is None


@pytest.mark.asyncio
async def test_bridge_skips_and_counts_already_set_disagreement(
    db_session: AsyncSession,
) -> None:
    """An affiliation already carrying team_program_id is never repointed.

    Per hard constraint D3, this backfill only ever moves a row from "target
    unset" to "target set" -- it never repoints or nulls an existing value,
    even when the participation bridge would resolve to a *different*
    program. That disagreement is counted, not silently applied, because a
    nonzero count signals the bridge and an already-set value disagree.
    """
    real_team_id = await _seed_team_and_program(
        db_session, slug="test-bridge-skip-real", abbreviation="BSR"
    )
    real_program_id = await _team_program_id_for(db_session, real_team_id)

    other_team_id = await _seed_team_and_program(
        db_session, slug="test-bridge-skip-other", abbreviation="BSO"
    )
    other_program_id = await _team_program_id_for(db_session, other_team_id)
    assert real_program_id != other_program_id

    edition_id = await _seed_edition(db_session)
    # The team entry resolves to `other_program_id` via the bridge.
    team_entry_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9004",
        nba_team_id=other_team_id,
        team_program_id=other_program_id,
    )
    source_player_id = await _seed_source_player(db_session, nba_stats_person_id="BR4")

    # But the affiliation already carries `real_program_id` (e.g. set by a
    # different, already-run resolution path).
    affiliation_id = await _seed_affiliation(db_session, team_program_id=real_program_id)
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        affiliation_id=affiliation_id,
    )

    report = await run_backfill(db_session)
    assert report.bridge.updated == 0
    assert report.bridge.already_set_disagreement == 1

    db_session.expunge_all()
    unchanged = (
        await db_session.execute(
            text("SELECT team_program_id FROM player_affiliations WHERE id = :id"),
            {"id": affiliation_id},
        )
    ).scalar_one()
    assert unchanged == real_program_id


@pytest.mark.asyncio
async def test_bridge_refuses_conflicting_participation_targets(
    db_session: AsyncSession,
) -> None:
    """Two participation rows disagreeing on one affiliation's target refuse, not guess.

    Mirrors this repo's entity-resolution rule (ambiguous -> refuse, never
    guess): if two participation rows both point their ``affiliation_id`` at
    the same row but their team entries resolve to different programs, the
    bridge must not silently pick one.
    """
    team_a_id = await _seed_team_and_program(
        db_session, slug="test-bridge-conflict-a", abbreviation="BCA"
    )
    program_a_id = await _team_program_id_for(db_session, team_a_id)
    team_b_id = await _seed_team_and_program(
        db_session, slug="test-bridge-conflict-b", abbreviation="BCB"
    )
    program_b_id = await _team_program_id_for(db_session, team_b_id)
    assert program_a_id != program_b_id

    edition_id = await _seed_edition(db_session)
    team_entry_a_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9005",
        nba_team_id=team_a_id,
        team_program_id=program_a_id,
    )
    team_entry_b_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9006",
        nba_team_id=team_b_id,
        team_program_id=program_b_id,
    )
    source_player_id = await _seed_source_player(db_session, nba_stats_person_id="BR5")

    affiliation_id = await _seed_affiliation(db_session)
    # Two participation rows (different stints) both point at the same
    # affiliation id, but via team entries resolving to different programs.
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_a_id,
        source_player_id=source_player_id,
        affiliation_id=affiliation_id,
        stint_no=1,
    )
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_b_id,
        source_player_id=source_player_id,
        affiliation_id=affiliation_id,
        stint_no=2,
    )

    report = await run_backfill(db_session)
    assert report.bridge.updated == 0
    assert report.bridge.ambiguous_participation == 1

    db_session.expunge_all()
    unchanged = (
        await db_session.execute(
            text("SELECT team_program_id FROM player_affiliations WHERE id = :id"),
            {"id": affiliation_id},
        )
    ).scalar_one()
    assert unchanged is None


@pytest.mark.asyncio
async def test_bridge_dry_run_reports_without_writing(
    db_session: AsyncSession,
) -> None:
    """``--dry-run`` counts what the bridge would do and writes nothing.

    The existing dry-run test seeds no ``SummerLeagueParticipation`` rows, so
    the bridge is inert there and ``report.bridge`` is never asserted under
    ``dry_run=True`` anywhere else in this file. That left the ``not dry_run``
    guard in ``run_backfill`` untested: deleting it would make ``--dry-run``
    silently write every eligible affiliation in production -- a preview
    command mutating 3,000+ rows -- with the whole suite still green.

    This is the test that fails if that guard is removed.
    """
    team_id = await _seed_team_and_program(db_session, slug="test-bridge-dry")
    team_program_id = await _team_program_id_for(db_session, team_id)

    edition_id = await _seed_edition(db_session)
    team_entry_id = await _seed_team_entry(
        db_session,
        competition_id=edition_id,
        nba_stats_team_id="9007",
        nba_team_id=team_id,
        team_program_id=team_program_id,
    )
    source_player_id = await _seed_source_player(db_session, nba_stats_person_id="BR6")
    affiliation_id = await _seed_affiliation(db_session)
    await _seed_participation(
        db_session,
        competition_id=edition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        affiliation_id=affiliation_id,
    )

    report = await run_backfill(db_session, dry_run=True)

    # Reports what a real run would do...
    assert report.bridge.eligible == 1
    # ...but claims no writes.
    assert report.bridge.updated == 0

    # And made none. Raw SQL after expunge_all(), so the identity map cannot
    # answer with the pre-run in-memory object and mask a write.
    db_session.expunge_all()
    still_null = (
        await db_session.execute(
            text("SELECT team_program_id FROM player_affiliations WHERE id = :id"),
            {"id": affiliation_id},
        )
    ).scalar_one()
    assert still_null is None

    # A real run afterwards still finds the row, proving the dry run did not
    # merely fail to resolve it.
    real = await run_backfill(db_session)
    assert real.bridge.updated == 1

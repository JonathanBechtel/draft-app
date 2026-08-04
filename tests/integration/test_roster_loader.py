"""Integration tests for the idempotent roster loader.

Exercises ``load_roster_snapshot`` against a real Postgres test schema and
asserts the six high-value invariants from the test plan:

1. ``test_first_load`` — first call creates one source player, one ANNOUNCED
   assertion, and one participation row per rostered player.
2. ``test_reload_idempotent`` — re-loading the same roster creates NO new
   assertion or participation rows (exact row counts unchanged).
3. ``test_late_add`` — adding one new player creates exactly one new ANNOUNCED
   assertion and one participation; others are untouched.
4. ``test_drop_supersedes_not_deletes`` — a dropped player gets a new CUT
   assertion pointing to the prior row; the prior row is retained with
   ``superseded_at`` set (never deleted).
5. ``test_history_reconstruction`` — point-in-time roster membership is
   reconstructable from the assertion stream alone.
6. ``test_diff_report`` — the diff report emits correct per-team
   added/unchanged/cut totals.

Requires ``TEST_DATABASE_URL``, ``PYTEST_ALLOW_DB=1``, and empty
``GEMINI_API_KEY``/``GEMINI_SUMMARIZATION_API_KEY``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.summer_league import (
    SummerLeagueParticipation,
    SummerLeagueSourceRecord,
)
from app.services.summer_league.roster_ingest import (
    CompetitionKey,
    _upsert_roster_competition,
    _upsert_roster_source_player,
    _upsert_roster_team_entry,
    load_roster_snapshot,
)
from app.services.summer_league.roster_changes import changed_source_player_ids
from app.services.summer_league.roster_parse import RosterEntry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMPETITION = CompetitionKey(year=2026, league_id="15", venue_slug="las_vegas")
TEAM_A = "1610612739"  # arbitrary nba_stats_team_id
TEAM_B = "1610612747"

T0 = datetime(2026, 7, 1, 0, 0, 0)
T1 = T0 + timedelta(days=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(person_id: str, team_id: str = TEAM_A) -> RosterEntry:
    """Build a minimal ``RosterEntry`` for testing.

    Args:
        person_id: NBA Stats person ID (used as a unique anchor).
        team_id: NBA Stats team ID; defaults to ``TEAM_A``.

    Returns:
        A ``RosterEntry`` with predictable test values.
    """
    return RosterEntry(
        nba_stats_person_id=person_id,
        raw_player_name=f"Player {person_id}",
        team_id=team_id,
        jersey="0",
        position="G",
        height="6-3",
        weight="185",
        birth_date=None,
        school=None,
        how_acquired=None,
        league_id="15",
    )


async def _aff_count(db: AsyncSession) -> int:
    """Return total PlayerAffiliation row count."""
    result = await db.execute(select(func.count()).select_from(PlayerAffiliation))
    return int(result.scalar() or 0)


async def _part_count(db: AsyncSession) -> int:
    """Return total SummerLeagueParticipation row count."""
    result = await db.execute(
        select(func.count()).select_from(SummerLeagueParticipation)
    )
    return int(result.scalar() or 0)


async def _sp_count(db: AsyncSession) -> int:
    """Return total SummerLeagueSourceRecord row count."""
    result = await db.execute(
        select(func.count()).select_from(SummerLeagueSourceRecord)
    )
    return int(result.scalar() or 0)


async def _seed_box_score_first_participation(
    db: AsyncSession,
    person_id: str,
    team_id: str,
    recorded_at: datetime,
) -> SummerLeagueParticipation:
    """Hand-seed a box-score-first participation, mirroring the normalization path.

    Reproduces ``normalization._ensure_participation``'s "born canonical" branch:
    a ``CONFIRMED`` ``player_affiliations`` row sourced from
    ``nba_summer_league_box_score`` plus a ``SummerLeagueParticipation`` bridge
    row pointing at it, with no prior ``ANNOUNCED`` roster assertion. Assumes the
    competition/team rows already exist (e.g. from a prior ``load_roster_snapshot``
    call for another player on the same team).

    Args:
        db: Async database session.
        person_id: NBA Stats person ID for the box-score-discovered player.
        team_id: NBA Stats team ID the player appeared for.
        recorded_at: Timestamp to stamp on the seeded CONFIRMED assertion.

    Returns:
        The seeded ``SummerLeagueParticipation`` row (flushed, with a PK).
    """
    competition_row = await _upsert_roster_competition(db, COMPETITION)
    await db.flush()
    competition_id: int = competition_row.id  # type: ignore[assignment]
    team_row = await _upsert_roster_team_entry(db, competition_id, team_id)
    await db.flush()
    source_player = await _upsert_roster_source_player(
        db, _entry(person_id, team_id), COMPETITION.year
    )
    await db.flush()

    box_score_affiliation = PlayerAffiliation(
        player_id=None,
        nba_team_id=None,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.CONFIRMED,
        recorded_at=recorded_at,
        source="nba_summer_league_box_score",
        source_ref=source_player.nba_stats_person_id,
    )
    db.add(box_score_affiliation)
    await db.flush()

    participation = SummerLeagueParticipation(
        competition_id=competition_id,
        team_entry_id=team_row.id,
        source_player_id=source_player.id,
        player_id=None,
        affiliation_id=box_score_affiliation.id,
        stint_no=1,
        roster_status=AffiliationStatus.CONFIRMED,
    )
    db.add(participation)
    await db.flush()
    return participation


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_load(db_session: AsyncSession) -> None:
    """First load creates one source player, one ANNOUNCED assertion, one participation per player.

    Asserts:
    - Two source-player rows are created (one per PERSON_ID).
    - Two ANNOUNCED affiliations are written, each with no supersession.
    - Two participation rows exist, linked to their respective affiliations.
    - The diff report shows added=2, unchanged=0, cut=0.
    """
    entries = [_entry("P1"), _entry("P2")]
    report = await load_roster_snapshot(
        db_session, COMPETITION, entries, recorded_at=T0
    )
    await db_session.commit()

    assert await _sp_count(db_session) == 2
    assert await _aff_count(db_session) == 2
    assert await _part_count(db_session) == 2

    aff_rows = (await db_session.execute(select(PlayerAffiliation))).scalars().all()
    for aff in aff_rows:
        assert aff.status == AffiliationStatus.ANNOUNCED
        assert aff.affiliation_type == AffiliationType.SUMMER_LEAGUE_ROSTER
        assert aff.superseded_at is None
        assert aff.retracted_at is None
        assert aff.source == "nba_summer_league_roster"

    assert report.added == 2
    assert report.unchanged == 0
    assert report.cut == 0


@pytest.mark.asyncio
async def test_reload_idempotent(db_session: AsyncSession) -> None:
    """Re-loading the same roster creates no new assertions or participation rows.

    Asserts:
    - Exact row counts for source_players, affiliations, participations are
      unchanged after the second load.
    - The diff report shows added=0, unchanged=2, cut=0.
    """
    entries = [_entry("P1"), _entry("P2")]

    await load_roster_snapshot(db_session, COMPETITION, entries, recorded_at=T0)
    await db_session.commit()

    # Reload the identical roster snapshot.
    report = await load_roster_snapshot(
        db_session, COMPETITION, entries, recorded_at=T1
    )
    await db_session.commit()

    assert await _sp_count(db_session) == 2
    assert await _aff_count(db_session) == 2
    assert await _part_count(db_session) == 2

    assert report.added == 0
    assert report.unchanged == 2
    assert report.cut == 0


@pytest.mark.asyncio
async def test_late_add(db_session: AsyncSession) -> None:
    """Adding one new player creates exactly one new ANNOUNCED assertion and participation.

    Asserts:
    - After the late-add reload: 3 total affiliations (2 original + 1 new).
    - The new assertion is ANNOUNCED with no supersedes_id.
    - The diff report shows added=1, unchanged=2, cut=0.
    """
    initial = [_entry("P1"), _entry("P2")]
    await load_roster_snapshot(db_session, COMPETITION, initial, recorded_at=T0)
    await db_session.commit()

    updated = [_entry("P1"), _entry("P2"), _entry("P3")]
    report = await load_roster_snapshot(
        db_session, COMPETITION, updated, recorded_at=T1
    )
    await db_session.commit()

    assert await _aff_count(db_session) == 3
    assert await _part_count(db_session) == 3

    # The newly created assertion is for P3, stamped at T1.
    new_affs = (
        (
            await db_session.execute(
                select(PlayerAffiliation).where(
                    PlayerAffiliation.recorded_at == T1,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(new_affs) == 1
    assert new_affs[0].status == AffiliationStatus.ANNOUNCED
    assert new_affs[0].supersedes_id is None

    assert report.added == 1
    assert report.unchanged == 2
    assert report.cut == 0


@pytest.mark.asyncio
async def test_drop_supersedes_not_deletes(db_session: AsyncSession) -> None:
    """Dropped player: CUT assertion added; prior row retained with superseded_at.

    This is the highest-value invariant test — it verifies the append-only
    contract directly.

    Asserts:
    - 3 total affiliations after the cut: the original 2 ANNOUNCED rows plus
      one new CUT row.
    - The CUT row has ``supersedes_id`` pointing to the prior ANNOUNCED row.
    - The prior row has ``superseded_at`` set to ``T1`` (NOT deleted).
    - Participation count remains 2 (the cut participation is updated, not removed).
    - The cut participation has ``roster_status == CUT``.
    - The diff report shows added=0, unchanged=1, cut=1.
    """
    initial = [_entry("P1"), _entry("P2")]
    await load_roster_snapshot(db_session, COMPETITION, initial, recorded_at=T0)
    await db_session.commit()

    # Drop P2.
    updated = [_entry("P1")]
    report = await load_roster_snapshot(
        db_session, COMPETITION, updated, recorded_at=T1
    )
    await db_session.commit()

    # Row counts: 2 original ANNOUNCED + 1 new CUT = 3 affiliations.
    assert await _aff_count(db_session) == 3
    # Participation rows are never deleted (still 2).
    assert await _part_count(db_session) == 2

    all_affs = (await db_session.execute(select(PlayerAffiliation))).scalars().all()
    announced = [a for a in all_affs if a.status == AffiliationStatus.ANNOUNCED]
    cut_affs = [a for a in all_affs if a.status == AffiliationStatus.CUT]

    assert len(announced) == 2  # both original rows retained
    assert len(cut_affs) == 1

    cut = cut_affs[0]
    assert cut.supersedes_id is not None

    # Verify the prior row has superseded_at set.
    prior = next(a for a in announced if a.id == cut.supersedes_id)
    assert prior.superseded_at is not None
    assert prior.superseded_at == T1

    # The participation for P2 must now be CUT.
    cut_parts = (
        (
            await db_session.execute(
                select(SummerLeagueParticipation).where(
                    SummerLeagueParticipation.roster_status  # type: ignore[arg-type]
                    == AffiliationStatus.CUT
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(cut_parts) == 1

    assert report.added == 0
    assert report.unchanged == 1
    assert report.cut == 1


@pytest.mark.asyncio
async def test_history_reconstruction(db_session: AsyncSession) -> None:
    """Point-in-time roster membership is reconstructable from the assertion stream.

    Sequence:
    - T0: load P1 + P2 (both ANNOUNCED).
    - T1: load P1 + P3 (P2 cut, P3 added).

    Asserts:
    - "Roster as of T0" query returns exactly {P1, P2} (by person id).
    - "Roster as of T1" query returns exactly {P1, P3} (P2 superseded at T1).

    Identity (not just counts) is asserted so a loader that superseded the
    *wrong* row (e.g. cut P1 instead of P2) would fail here.
    """
    # T0: P1 and P2 announced.
    await load_roster_snapshot(
        db_session, COMPETITION, [_entry("P1"), _entry("P2")], recorded_at=T0
    )
    await db_session.commit()

    # T1: P2 dropped, P3 added.
    await load_roster_snapshot(
        db_session, COMPETITION, [_entry("P1"), _entry("P3")], recorded_at=T1
    )
    await db_session.commit()

    async def _roster_person_ids_as_of(at: datetime) -> set[str]:
        """Reconstruct the active roster's person ids from the assertion stream.

        An ANNOUNCED assertion is "active at ``at``" when it was recorded on or
        before ``at`` and was not yet superseded as of ``at``. This queries the
        append-only assertion stream *directly* (not via participation, which
        only points at the current assertion). The affiliation's ``player_id``
        is null pre-resolution, so the person id is recovered from ``source_ref``
        (``"{league_id}/{team_id}/{person_id}"``), which the CUT row also carries.
        """
        rows = (
            (
                await db_session.execute(
                    select(PlayerAffiliation).where(
                        PlayerAffiliation.affiliation_type  # type: ignore[arg-type]
                        == AffiliationType.SUMMER_LEAGUE_ROSTER,
                        PlayerAffiliation.status != AffiliationStatus.CUT,  # type: ignore[arg-type]
                        PlayerAffiliation.recorded_at <= at,  # type: ignore[arg-type]
                        or_(
                            PlayerAffiliation.superseded_at.is_(None),  # type: ignore[union-attr]
                            PlayerAffiliation.superseded_at > at,  # type: ignore[arg-type,operator]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {(aff.source_ref or "").rsplit("/", 1)[-1] for aff in rows}

    # P1 and P2 were both announced at T0; P2 not yet superseded at T0.
    assert await _roster_person_ids_as_of(T0) == {"P1", "P2"}
    # P2's ANNOUNCED row was superseded at T1; P1 (still active) and P3 (new) remain.
    assert await _roster_person_ids_as_of(T1) == {"P1", "P3"}


@pytest.mark.asyncio
async def test_diff_report(db_session: AsyncSession) -> None:
    """Diff report emits correct per-team added/unchanged/cut totals.

    Scenario: two teams; after initial load, one player dropped and one added
    on Team A; Team B is unchanged.

    Asserts per-team and aggregate counts on both loads.
    """
    # Initial load: Team A has P1+P2, Team B has P3.
    initial = [_entry("P1", TEAM_A), _entry("P2", TEAM_A), _entry("P3", TEAM_B)]
    report1 = await load_roster_snapshot(
        db_session, COMPETITION, initial, recorded_at=T0
    )
    await db_session.commit()

    assert report1.added == 3
    assert report1.unchanged == 0
    assert report1.cut == 0
    assert TEAM_A in report1.per_team
    assert TEAM_B in report1.per_team
    assert report1.per_team[TEAM_A].added == 2
    assert report1.per_team[TEAM_A].unchanged == 0
    assert report1.per_team[TEAM_A].cut == 0
    assert report1.per_team[TEAM_B].added == 1
    assert report1.per_team[TEAM_B].unchanged == 0
    assert report1.per_team[TEAM_B].cut == 0

    # Second load: drop P1 from Team A, add P4 to Team A; Team B unchanged.
    v2 = [_entry("P2", TEAM_A), _entry("P4", TEAM_A), _entry("P3", TEAM_B)]
    report2 = await load_roster_snapshot(db_session, COMPETITION, v2, recorded_at=T1)
    await db_session.commit()

    assert report2.per_team[TEAM_A].added == 1
    assert report2.per_team[TEAM_A].unchanged == 1
    assert report2.per_team[TEAM_A].cut == 1
    assert report2.per_team[TEAM_B].added == 0
    assert report2.per_team[TEAM_B].unchanged == 1
    assert report2.per_team[TEAM_B].cut == 0
    assert report2.added == 1
    assert report2.unchanged == 2
    assert report2.cut == 1
    changed_ids = await changed_source_player_ids(
        db_session,
        year=COMPETITION.year,
        league_id=COMPETITION.league_id,
        recorded_at=T1,
    )
    assert len(changed_ids) == 2


@pytest.mark.asyncio
async def test_empty_team_cuts_all_players(db_session: AsyncSession) -> None:
    """A team absent from the snapshot receives CUT assertions for all active players.

    Scenario: Load TEAM_A with P1+P2 at T0, then reload with zero entries at T1.

    Asserts:
    - Both prior ANNOUNCED rows are retained with superseded_at=T1 (not deleted).
    - Two new CUT assertions are written, each supersedes_id pointing to a prior row.
    - Both participation rows have roster_status==CUT after T1 load.
    - Diff report shows cut=2.
    """
    initial = [_entry("P1", TEAM_A), _entry("P2", TEAM_A)]
    await load_roster_snapshot(db_session, COMPETITION, initial, recorded_at=T0)
    await db_session.commit()

    # Second snapshot has zero entries — TEAM_A is entirely absent.
    report = await load_roster_snapshot(db_session, COMPETITION, [], recorded_at=T1)
    await db_session.commit()

    # 2 original ANNOUNCED + 2 new CUT = 4 affiliations.
    assert await _aff_count(db_session) == 4
    # Participation rows are never deleted.
    assert await _part_count(db_session) == 2

    all_affs = (await db_session.execute(select(PlayerAffiliation))).scalars().all()
    announced = [a for a in all_affs if a.status == AffiliationStatus.ANNOUNCED]
    cut_affs = [a for a in all_affs if a.status == AffiliationStatus.CUT]

    assert len(announced) == 2
    assert len(cut_affs) == 2
    for cut in cut_affs:
        assert cut.supersedes_id is not None
        prior = next(a for a in announced if a.id == cut.supersedes_id)
        assert prior.superseded_at == T1

    # All participation rows are now CUT.
    parts = (
        (await db_session.execute(select(SummerLeagueParticipation))).scalars().all()
    )
    assert all(p.roster_status == AffiliationStatus.CUT for p in parts)

    assert report.added == 0
    assert report.unchanged == 0
    assert report.cut == 2


@pytest.mark.asyncio
async def test_readd_after_cut_reactivates(db_session: AsyncSession) -> None:
    """Re-adding a previously cut player reuses the existing participation row.

    Sequence: load P1 at T0 → cut at T1 → re-add at T2.

    Asserts:
    - No IntegrityError (no duplicate stint_no=1 row).
    - Exactly one participation row exists for P1.
    - roster_status returns to ANNOUNCED after T2 load.
    - Affiliation chain is ANNOUNCED(T0)→CUT(T1)→ANNOUNCED(T2) via supersedes_id.
    - The stable bridge points at the latest (T2) assertion.
    """
    T2 = T1 + timedelta(days=1)

    await load_roster_snapshot(db_session, COMPETITION, [_entry("P1")], recorded_at=T0)
    await db_session.commit()

    # Cut P1 by sending an empty snapshot.
    await load_roster_snapshot(db_session, COMPETITION, [], recorded_at=T1)
    await db_session.commit()

    # Re-add P1.
    await load_roster_snapshot(db_session, COMPETITION, [_entry("P1")], recorded_at=T2)
    await db_session.commit()

    # One stable participation row (never duplicated).
    assert await _part_count(db_session) == 1
    # Three assertions: ANNOUNCED → CUT → ANNOUNCED.
    assert await _aff_count(db_session) == 3

    part = (await db_session.execute(select(SummerLeagueParticipation))).scalar_one()
    assert part.roster_status == AffiliationStatus.ANNOUNCED

    all_affs = (
        (
            await db_session.execute(
                select(PlayerAffiliation).order_by(
                    PlayerAffiliation.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    aff_t0, aff_cut, aff_t2 = all_affs

    assert aff_t0.status == AffiliationStatus.ANNOUNCED
    assert aff_t0.superseded_at == T1

    assert aff_cut.status == AffiliationStatus.CUT
    assert aff_cut.supersedes_id == aff_t0.id
    assert aff_cut.superseded_at == T2

    assert aff_t2.status == AffiliationStatus.ANNOUNCED
    assert aff_t2.supersedes_id == aff_cut.id
    assert aff_t2.superseded_at is None

    # Participation points at the re-announce assertion.
    assert part.affiliation_id == aff_t2.id


@pytest.mark.asyncio
async def test_unchanged_refreshes_metadata(db_session: AsyncSession) -> None:
    """Unchanged players get updated jersey/position without a new affiliation row.

    Scenario: Load P1 with jersey "5", re-load P1 with jersey "10".

    Asserts:
    - No new affiliation or participation rows after the second load.
    - participation.jersey_number == "10" after the reload.
    """
    from app.services.summer_league.roster_parse import RosterEntry

    def _entry_jersey(jersey: str) -> RosterEntry:
        return RosterEntry(
            nba_stats_person_id="P1",
            raw_player_name="Player P1",
            team_id=TEAM_A,
            jersey=jersey,
            position="G",
            height="6-3",
            weight="185",
            birth_date=None,
            school=None,
            how_acquired=None,
            league_id="15",
        )

    await load_roster_snapshot(
        db_session, COMPETITION, [_entry_jersey("5")], recorded_at=T0
    )
    await db_session.commit()

    await load_roster_snapshot(
        db_session, COMPETITION, [_entry_jersey("10")], recorded_at=T1
    )
    await db_session.commit()

    # No new assertion rows written for a metadata-only change.
    assert await _aff_count(db_session) == 1
    assert await _part_count(db_session) == 1

    part = (await db_session.execute(select(SummerLeagueParticipation))).scalar_one()
    assert part.jersey_number == "10"


@pytest.mark.asyncio
async def test_heal_box_score_first_gains_announced_assertion(
    db_session: AsyncSession,
) -> None:
    """A box-score-first participation gains an ANNOUNCED assertion on later load.

    Scenario: P2 is discovered box-score-first (CONFIRMED, ``nba_summer_league_box_score``
    source, no prior ANNOUNCED row) alongside a normally-announced P1. A roster
    snapshot listing both P1 and P2 is then loaded.

    Asserts:
    - The box-score assertion is retained (not deleted) with ``superseded_at`` set.
    - A new ANNOUNCED ``nba_summer_league_roster`` assertion is appended, chained
      via ``supersedes_id`` to the box-score row.
    - ``roster_status`` stays ``CONFIRMED`` (heal does not touch the promotion
      semantics owned elsewhere); the bridge's ``affiliation_id`` moves to the
      new (latest) assertion.
    - Both players are classified ``unchanged`` (P2 already had an active,
      non-CUT participation before this load).
    """
    # Seed the competition/team via a normal announced player (P1).
    await load_roster_snapshot(db_session, COMPETITION, [_entry("P1")], recorded_at=T0)
    await db_session.commit()

    # Hand-seed P2 as box-score-first.
    seeded = await _seed_box_score_first_participation(db_session, "P2", TEAM_A, T0)
    box_score_aff_id = seeded.affiliation_id
    await db_session.commit()

    report = await load_roster_snapshot(
        db_session, COMPETITION, [_entry("P1"), _entry("P2")], recorded_at=T1
    )
    await db_session.commit()

    # 1 (P1 ANNOUNCED) + 1 (P2 box_score, now superseded) + 1 (P2 healed ANNOUNCED) = 3.
    assert await _aff_count(db_session) == 3
    assert await _part_count(db_session) == 2

    all_affs = (await db_session.execute(select(PlayerAffiliation))).scalars().all()

    box_aff = next(a for a in all_affs if a.id == box_score_aff_id)
    assert box_aff.source == "nba_summer_league_box_score"
    assert box_aff.status == AffiliationStatus.CONFIRMED
    assert box_aff.superseded_at == T1

    healed = next(a for a in all_affs if a.supersedes_id == box_score_aff_id)
    assert healed.status == AffiliationStatus.ANNOUNCED
    assert healed.source == "nba_summer_league_roster"
    assert healed.superseded_at is None

    part = (
        await db_session.execute(
            select(SummerLeagueParticipation).where(
                SummerLeagueParticipation.source_player_id  # type: ignore[arg-type]
                == seeded.source_player_id
            )
        )
    ).scalar_one()
    assert part.roster_status == AffiliationStatus.CONFIRMED
    assert part.affiliation_id == healed.id

    assert report.added == 0
    assert report.unchanged == 2
    assert report.cut == 0


@pytest.mark.asyncio
async def test_heal_is_idempotent_across_reloads(db_session: AsyncSession) -> None:
    """Re-loading the same snapshot after a heal creates no duplicate assertion.

    Sequence: seed a box-score-first participation, load a snapshot that heals
    it, then reload the identical snapshot again.

    Asserts:
    - Affiliation and participation row counts are unchanged after the second load.
    - Exactly one ANNOUNCED assertion supersedes the box-score row (no duplicate).
    """
    await load_roster_snapshot(db_session, COMPETITION, [_entry("P1")], recorded_at=T0)
    await db_session.commit()

    seeded = await _seed_box_score_first_participation(db_session, "P2", TEAM_A, T0)
    box_score_aff_id = seeded.affiliation_id
    await db_session.commit()

    entries = [_entry("P1"), _entry("P2")]

    await load_roster_snapshot(db_session, COMPETITION, entries, recorded_at=T1)
    await db_session.commit()

    aff_count_after_heal = await _aff_count(db_session)
    part_count_after_heal = await _part_count(db_session)

    # Reload the identical snapshot.
    T2 = T1 + timedelta(days=1)
    report = await load_roster_snapshot(
        db_session, COMPETITION, entries, recorded_at=T2
    )
    await db_session.commit()

    assert await _aff_count(db_session) == aff_count_after_heal
    assert await _part_count(db_session) == part_count_after_heal

    all_affs = (await db_session.execute(select(PlayerAffiliation))).scalars().all()
    healed_affs = [a for a in all_affs if a.supersedes_id == box_score_aff_id]
    assert len(healed_affs) == 1

    assert report.added == 0
    assert report.unchanged == 2
    assert report.cut == 0


@pytest.mark.asyncio
async def test_heal_does_not_affect_normal_announced_players(
    db_session: AsyncSession,
) -> None:
    """Normal announced-only players are unaffected by the box-score heal branch.

    Scenario: P1 is announced normally (no box-score-first participation exists
    for anyone) and its snapshot is reloaded unchanged.

    Asserts:
    - No extra affiliation rows are written by the heal branch.
    - The sole affiliation for P1 remains the original ANNOUNCED row, unsuperseded.
    """
    await load_roster_snapshot(db_session, COMPETITION, [_entry("P1")], recorded_at=T0)
    await db_session.commit()

    await load_roster_snapshot(db_session, COMPETITION, [_entry("P1")], recorded_at=T1)
    await db_session.commit()

    assert await _aff_count(db_session) == 1
    aff = (await db_session.execute(select(PlayerAffiliation))).scalar_one()
    assert aff.status == AffiliationStatus.ANNOUNCED
    assert aff.source == "nba_summer_league_roster"
    assert aff.superseded_at is None

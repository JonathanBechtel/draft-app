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
    SummerLeagueSourcePlayer,
)
from app.services.summer_league.roster_ingest import (
    CompetitionKey,
    load_roster_snapshot,
)
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
    result = await db.execute(
        select(func.count()).select_from(PlayerAffiliation)
    )
    return int(result.scalar() or 0)


async def _part_count(db: AsyncSession) -> int:
    """Return total SummerLeagueParticipation row count."""
    result = await db.execute(
        select(func.count()).select_from(SummerLeagueParticipation)
    )
    return int(result.scalar() or 0)


async def _sp_count(db: AsyncSession) -> int:
    """Return total SummerLeagueSourcePlayer row count."""
    result = await db.execute(
        select(func.count()).select_from(SummerLeagueSourcePlayer)
    )
    return int(result.scalar() or 0)


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

    aff_rows = (
        await db_session.execute(select(PlayerAffiliation))
    ).scalars().all()
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
        await db_session.execute(
            select(PlayerAffiliation).where(
                PlayerAffiliation.recorded_at == T1,  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
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

    all_affs = (
        await db_session.execute(select(PlayerAffiliation))
    ).scalars().all()
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
        await db_session.execute(
            select(SummerLeagueParticipation).where(
                SummerLeagueParticipation.roster_status  # type: ignore[arg-type]
                == AffiliationStatus.CUT
            )
        )
    ).scalars().all()
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
    - "Roster as of T0" query returns 2 players (P1, P2 — P2 not yet superseded).
    - "Roster as of T1" query returns 2 players (P1, P3 — P2 superseded at T1).
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

    # --- Point-in-time query: "who was on the roster as of T0?" ---
    # An ANNOUNCED assertion is "active at T" if:
    #   recorded_at <= T
    #   AND (superseded_at IS NULL OR superseded_at > T)
    #   AND status != CUT
    as_of_t0 = (
        await db_session.execute(
            select(PlayerAffiliation).where(
                PlayerAffiliation.affiliation_type  # type: ignore[arg-type]
                == AffiliationType.SUMMER_LEAGUE_ROSTER,
                PlayerAffiliation.status != AffiliationStatus.CUT,  # type: ignore[arg-type]
                PlayerAffiliation.recorded_at <= T0,  # type: ignore[arg-type]
                or_(
                    PlayerAffiliation.superseded_at.is_(None),  # type: ignore[union-attr]
                    PlayerAffiliation.superseded_at > T0,  # type: ignore[arg-type,operator]
                ),
            )
        )
    ).scalars().all()
    # P1 and P2 were both announced at T0; P2 not yet superseded at T0.
    assert len(as_of_t0) == 2

    # --- Point-in-time query: "who was on the roster as of T1?" ---
    as_of_t1 = (
        await db_session.execute(
            select(PlayerAffiliation).where(
                PlayerAffiliation.affiliation_type  # type: ignore[arg-type]
                == AffiliationType.SUMMER_LEAGUE_ROSTER,
                PlayerAffiliation.status != AffiliationStatus.CUT,  # type: ignore[arg-type]
                PlayerAffiliation.recorded_at <= T1,  # type: ignore[arg-type]
                or_(
                    PlayerAffiliation.superseded_at.is_(None),  # type: ignore[union-attr]
                    PlayerAffiliation.superseded_at > T1,  # type: ignore[arg-type,operator]
                ),
            )
        )
    ).scalars().all()
    # P2's ANNOUNCED row was superseded at T1 (not > T1), so it is excluded.
    # P1 (announced T0, still active) and P3 (announced T1) remain.
    assert len(as_of_t1) == 2


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
    report2 = await load_roster_snapshot(
        db_session, COMPETITION, v2, recorded_at=T1
    )
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

"""Integration tests for T4 roster player resolution and canonical-id backfill.

Covers the three high-value invariants from the test plan:

1. ``test_external_id_match`` — PERSON_ID with an existing ``nba_stats``
   external id resolves with status ``EXTERNAL_ID`` and the correct
   ``player_id`` (deterministic, no fuzzy matching).
2. ``test_stub_vs_unresolved`` — an unmatched player gets status ``STUB`` and
   an ``is_stub`` canonical record when ``create_stub=True``; without the flag
   the status is ``UNRESOLVED`` and no canonical link is created.
3. ``test_backfill_canonical_ids`` — after resolution, ``player_id`` is
   backfilled onto ``summer_league_participation`` and the linked
   ``player_affiliations`` row.

Requires ``TEST_DATABASE_URL``, ``PYTEST_ALLOW_DB=1``, and empty
``GEMINI_API_KEY`` / ``GEMINI_SUMMARIZATION_API_KEY`` (disables vector search;
resolver degrades to lexical-only, which is acceptable here).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import PlayerAffiliation, AffiliationStatus
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueParticipation,
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
)
from app.services.backbone.player_resolution import resolve_source_player
from app.services.sources.summer_league.roster_ingest import (
    CompetitionKey,
    load_roster_snapshot,
)
from app.services.sources.summer_league.roster_parse import RosterEntry

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

COMPETITION = CompetitionKey(year=2026, league_id="15", venue_slug="las_vegas")
TEAM_A = "1610612739"
T0 = datetime(2026, 7, 1, 0, 0, 0)


def _make_player(display_name: str = "Test Player") -> PlayerMaster:
    """Build an unsaved canonical PlayerMaster for testing."""
    parts = display_name.split(" ", 1)
    return PlayerMaster(
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        display_name=display_name,
        is_stub=False,
        bio_source="test",
    )


def _make_source_player(
    person_id: str, name: str = "Test Player"
) -> SummerLeagueSourceRecord:
    """Build an unsaved SummerLeagueSourceRecord for testing."""
    return SummerLeagueSourceRecord(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        normalized_name=name.lower(),
        first_seen_year=2026,
        last_seen_year=2026,
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )


def _entry(person_id: str, name: str = "Test Player") -> RosterEntry:
    """Build a minimal ``RosterEntry`` for roster-loader tests."""
    return RosterEntry(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        team_id=TEAM_A,
        jersey="0",
        position="G",
        height="6-3",
        weight="185",
        birth_date=None,
        school=None,
        how_acquired=None,
        league_id="15",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_id_match(db_session: AsyncSession) -> None:
    """PERSON_ID with existing nba_stats external id → deterministic EXTERNAL_ID resolution.

    Creates a canonical player and links it to PERSON_001 via
    ``player_external_ids``. Resolving the matching source player must
    return status ``EXTERNAL_ID``, the correct ``player_id``, and no stub.
    """
    # Canonical player.
    player = _make_player("External Id Player")
    db_session.add(player)
    await db_session.flush()

    # Wire the nba_stats external ID.
    ext_id = PlayerExternalId(
        player_id=player.id,
        system="nba_stats",
        external_id="EXT_PERSON_001",
    )
    db_session.add(ext_id)
    await db_session.flush()

    # Source player with the same PERSON_ID.
    source_player = _make_source_player("EXT_PERSON_001", "External Id Player")
    db_session.add(source_player)
    await db_session.flush()

    result = await resolve_source_player(db_session, source_player, create_stub=False)
    await db_session.commit()

    assert result.status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert result.player_id == player.id
    assert result.resolved
    assert not result.stub_created

    # The source player row must be updated.
    assert source_player.canonical_player_id == player.id
    assert source_player.resolution_status == SummerLeagueResolutionStatus.EXTERNAL_ID


@pytest.mark.asyncio
async def test_stub_vs_unresolved(db_session: AsyncSession) -> None:
    """Unmatched player + create_stub=True → STUB is_stub record; without flag → UNRESOLVED.

    Uses a uniquely gibberish name with no external ID or alias so neither the
    exact nor the lexical candidate path can find a match, driving the test to
    the final stub/unresolved fork.
    """
    source_player = _make_source_player(
        "UNMATCHED_9999",
        "Zqxjvv Unresolvable Bogusname",
    )
    db_session.add(source_player)
    await db_session.flush()

    # Without create_stub: expect UNRESOLVED with no canonical link.
    result_no_stub = await resolve_source_player(
        db_session, source_player, create_stub=False
    )
    await db_session.commit()

    assert result_no_stub.status == SummerLeagueResolutionStatus.UNRESOLVED
    assert result_no_stub.player_id is None
    assert not result_no_stub.resolved
    assert not result_no_stub.stub_created

    # Reset to UNRESOLVED so the second call enters the cascade fresh.
    source_player.resolution_status = SummerLeagueResolutionStatus.UNRESOLVED
    source_player.canonical_player_id = None
    source_player.resolution_confidence = None
    source_player.resolution_candidates = None
    db_session.add(source_player)
    await db_session.flush()

    # With create_stub=True: expect a new is_stub canonical player, status STUB.
    result_stub = await resolve_source_player(
        db_session, source_player, create_stub=True
    )
    await db_session.commit()

    assert result_stub.status == SummerLeagueResolutionStatus.STUB
    assert result_stub.player_id is not None
    assert result_stub.resolved
    assert result_stub.stub_created

    stub = await db_session.get(PlayerMaster, result_stub.player_id)
    assert stub is not None
    assert stub.is_stub is True
    assert stub.bio_source == "summer_league_ingest"


@pytest.mark.asyncio
async def test_backfill_canonical_ids(db_session: AsyncSession) -> None:
    """Resolution backfills player_id onto participation and affiliation rows.

    Sequence:
    - Load a roster snapshot for PERSON_BACKFILL_01, creating a source player,
      an ANNOUNCED PlayerAffiliation, and a SummerLeagueParticipation row.
    - Create a canonical player linked via nba_stats external id.
    - Resolve the source player (EXTERNAL_ID path).
    - Assert that ``summer_league_participation.player_id`` and the linked
      ``player_affiliations.player_id`` both equal the canonical player's id.
    """
    # Create a roster snapshot so participation + affiliation rows exist.
    entries = [_entry("PERSON_BACKFILL_01", "Backfill Test Player")]
    await load_roster_snapshot(db_session, COMPETITION, entries, recorded_at=T0)
    await db_session.commit()

    # Sanity: participation.player_id should be NULL before resolution.
    part_result = await db_session.execute(select(SummerLeagueParticipation))
    participations = part_result.scalars().all()
    assert len(participations) == 1
    assert participations[0].player_id is None

    aff_result = await db_session.execute(select(PlayerAffiliation))
    affiliations = aff_result.scalars().all()
    assert len(affiliations) == 1
    assert affiliations[0].player_id is None

    # Create canonical player and wire nba_stats external id.
    canonical = _make_player("Backfill Test Player")
    db_session.add(canonical)
    await db_session.flush()

    ext_id = PlayerExternalId(
        player_id=canonical.id,
        system="nba_stats",
        external_id="PERSON_BACKFILL_01",
    )
    db_session.add(ext_id)
    await db_session.flush()

    # Load the source player created by the roster loader.
    sp_result = await db_session.execute(
        select(SummerLeagueSourceRecord).where(
            SummerLeagueSourceRecord.nba_stats_person_id == "PERSON_BACKFILL_01"  # type: ignore[arg-type]
        )
    )
    source_player = sp_result.scalar_one()

    # Resolve.
    result = await resolve_source_player(db_session, source_player)
    await db_session.commit()

    assert result.status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert result.player_id == canonical.id
    assert result.participations_backfilled == 1

    # Verify participation.player_id was backfilled.
    await db_session.refresh(participations[0])
    assert participations[0].player_id == canonical.id

    # Verify affiliation.player_id was backfilled.
    await db_session.refresh(affiliations[0])
    assert affiliations[0].player_id == canonical.id


@pytest.mark.asyncio
async def test_backfill_walks_supersedes_chain_for_cut_player(
    db_session: AsyncSession,
) -> None:
    """Resolution backfills the superseded ANNOUNCED ancestor of a CUT player.

    Exercises the ``supersedes_id`` chain-walk in
    ``_backfill_participation_and_affiliation``: when a player is cut, the
    participation's current pointer is the CUT assertion, while the prior
    ANNOUNCED assertion is reachable only via ``supersedes_id``. Both must be
    backfilled so the historical assertion is not left with a NULL ``player_id``.

    Sequence:
    - Load v1 with PERSON_CUT_01 on TEAM_A → one ANNOUNCED assertion.
    - Load v2 with a different player on TEAM_A → PERSON_CUT_01 is cut
      (new CUT assertion supersedes the ANNOUNCED row).
    - Resolve PERSON_CUT_01 via its nba_stats external id.

    Asserts:
    - Both PERSON_CUT_01 assertions (ANNOUNCED ancestor + CUT) get the canonical
      ``player_id``.
    - The unrelated other player's assertion is NOT backfilled (scoped to the
      resolved source player only).
    """
    # v1: PERSON_CUT_01 announced on TEAM_A.
    await load_roster_snapshot(
        db_session, COMPETITION, [_entry("PERSON_CUT_01", "Cut Player")], recorded_at=T0
    )
    await db_session.commit()

    # v2: PERSON_CUT_01 dropped, a different player added → CUT supersedes ANNOUNCED.
    await load_roster_snapshot(
        db_session,
        COMPETITION,
        [_entry("PERSON_OTHER_02", "Other Player")],
        recorded_at=datetime(2026, 7, 2, 0, 0, 0),
    )
    await db_session.commit()

    # Two assertions exist for the cut player: the superseded ANNOUNCED + the CUT.
    cut_player_affs = (
        (
            await db_session.execute(
                select(PlayerAffiliation).where(
                    PlayerAffiliation.source_ref.like("%/PERSON_CUT_01")  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(cut_player_affs) == 2
    assert {a.status for a in cut_player_affs} == {
        AffiliationStatus.ANNOUNCED,
        AffiliationStatus.CUT,
    }
    assert all(a.player_id is None for a in cut_player_affs)

    # Canonical player + nba_stats external id for the cut player.
    canonical = _make_player("Cut Player")
    db_session.add(canonical)
    await db_session.flush()
    db_session.add(
        PlayerExternalId(
            player_id=canonical.id,
            system="nba_stats",
            external_id="PERSON_CUT_01",
        )
    )
    await db_session.flush()

    source_player = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "PERSON_CUT_01"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    result = await resolve_source_player(db_session, source_player)
    await db_session.commit()

    assert result.status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert result.player_id == canonical.id

    # BOTH the CUT row and its superseded ANNOUNCED ancestor must be backfilled.
    for aff in cut_player_affs:
        await db_session.refresh(aff)
        assert aff.player_id == canonical.id

    # The unrelated player's assertion stays NULL (backfill is source-scoped).
    other_aff = (
        await db_session.execute(
            select(PlayerAffiliation).where(
                PlayerAffiliation.source_ref.like("%/PERSON_OTHER_02")  # type: ignore[union-attr]
            )
        )
    ).scalar_one()
    assert other_aff.player_id is None

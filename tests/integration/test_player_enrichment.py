"""Integration tests for the player enrichment service."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select, text

from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.players_master import PlayerMaster
from app.services.player_enrichment_service import _apply_bio_data, _apply_stats_data


def _make_stub(name: str, **kwargs) -> PlayerMaster:  # type: ignore[no-untyped-def]
    """Build an unsaved stub PlayerMaster."""
    first, last = name.split(" ", 1)
    return PlayerMaster(
        first_name=first,
        last_name=last,
        display_name=name,
        is_stub=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1: Bio persistence with confidence gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_bio_high_confidence_persists(db_session: AsyncSession) -> None:
    """High-confidence bio response populates PlayerMaster fields."""
    player = _make_stub("Test Prospect")
    db_session.add(player)
    await db_session.flush()

    data = {
        "confidence": "high",
        "birthdate": "2005-03-15",
        "birth_city": "Chicago",
        "birth_state_province": "Illinois",
        "birth_country": "USA",
        "school": "Duke",
        "high_school": "Simeon",
        "shoots": "Right",
        "draft_year": 2026,
        "rsci_rank": 5,
    }

    await _apply_bio_data(db_session, player, data)

    assert player.school == "Duke"
    assert player.birth_city == "Chicago"
    assert player.high_school == "Simeon"
    assert player.rsci_rank == 5
    assert player.bio_source == "ai_generated"
    assert str(player.birthdate) == "2005-03-15"


@pytest.mark.asyncio
async def test_apply_bio_low_confidence_skipped(db_session: AsyncSession) -> None:
    """Low-confidence bio response does not modify the player record."""
    player = _make_stub("Test Prospect")
    db_session.add(player)
    await db_session.flush()

    data = {
        "confidence": "low",
        "school": "Duke",
        "birth_city": "Chicago",
    }

    await _apply_bio_data(db_session, player, data)

    assert player.school is None
    assert player.birth_city is None
    assert player.bio_source is None


# ---------------------------------------------------------------------------
# Test 2: Stats upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_insert_and_upsert(db_session: AsyncSession) -> None:
    """Stats are inserted on first call and updated (not duplicated) on second."""
    player = _make_stub("Test Prospect")
    db_session.add(player)
    await db_session.flush()

    data_v1 = {
        "season": "2025-26",
        "stats": {"ppg": 18.5, "rpg": 6.0, "apg": 3.2, "games": 30},
    }
    await _apply_stats_data(db_session, player, data_v1)
    await db_session.flush()

    # Verify initial insert
    result = await db_session.execute(
        select(PlayerCollegeStats).where(
            PlayerCollegeStats.player_id == player.id
        )
    )
    stats = result.scalar_one()
    assert stats.ppg == 18.5
    assert stats.games == 30

    # Capture ID before upsert to avoid lazy-load issues
    player_id = player.id

    # Upsert with updated values
    data_v2 = {
        "season": "2025-26",
        "stats": {"ppg": 20.1, "rpg": 6.5, "apg": 3.5, "games": 34},
    }
    await _apply_stats_data(db_session, player, data_v2)
    await db_session.flush()

    # Verify update via raw SQL to bypass ORM identity map cache
    row = (
        await db_session.execute(
            text(
                "SELECT ppg, games FROM player_college_stats"
                " WHERE player_id = :pid"
            ),
            {"pid": player_id},
        )
    ).one()
    assert row.ppg == 20.1
    assert row.games == 34

    # Verify no duplicate rows
    count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM player_college_stats"
                " WHERE player_id = :pid"
            ),
            {"pid": player_id},
        )
    ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Test 3: Only-fill-empty-fields rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_bio_preserves_existing_fields(db_session: AsyncSession) -> None:
    """Bio enrichment does not overwrite fields that already have values."""
    player = _make_stub("Test Prospect", school="Kentucky", draft_year=2025)
    db_session.add(player)
    await db_session.flush()

    data = {
        "confidence": "high",
        "school": "Duke",  # different from existing
        "draft_year": 2026,  # different from existing
        "birth_city": "Atlanta",  # new field, should be filled
        "rsci_rank": 3,  # new field, should be filled
    }

    await _apply_bio_data(db_session, player, data)

    # Existing fields preserved
    assert player.school == "Kentucky"
    assert player.draft_year == 2025
    # Empty fields filled
    assert player.birth_city == "Atlanta"
    assert player.rsci_rank == 3
    assert player.bio_source == "ai_generated"

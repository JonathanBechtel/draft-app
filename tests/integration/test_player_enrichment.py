"""Integration tests for the player enrichment service."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.services.player_enrichment_service import _apply_bio_data


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

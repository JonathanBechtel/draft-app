"""Integration tests for the player enrichment service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sqlalchemy import select, text

from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.players_master import PlayerMaster
from app.services import player_enrichment_service
from app.services.player_enrichment_service import (
    EnrichmentResult,
    _apply_bio_data,
    _apply_stats_data,
    run_enrichment_sweep,
)


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


# ---------------------------------------------------------------------------
# Test 4: Sweep query filters — only unenriched stubs are processed
# ---------------------------------------------------------------------------

_MOCK_BIO = {
    "confidence": "high",
    "school": "Test U",
    "season": "2025-26",
    "stats": {"ppg": 10.0, "games": 20},
}


def _schema_aware_factory(
    base_factory: async_sessionmaker[AsyncSession],
    schema: str,
) -> async_sessionmaker[AsyncSession]:
    """Wrap a session factory so every session sets search_path."""

    @asynccontextmanager
    async def _make_session() -> AsyncGenerator[AsyncSession, None]:
        async with base_factory() as session:
            await session.execute(text(f'SET search_path TO "{schema}"'))
            await session.commit()
            yield session

    # run_enrichment_sweep calls session_factory() as a context manager,
    # so we return an object whose __call__ returns the async CM.
    class _Factory:
        def __call__(self) -> AsyncGenerator[AsyncSession, None]:
            return _make_session()  # type: ignore[return-value]

    return _Factory()  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_sweep_only_processes_unenriched_stubs(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """Sweep skips non-stubs, already-enriched stubs, and only processes eligible ones."""
    wrapped = _schema_aware_factory(session_factory, test_schema)

    # Seed three players
    async with session_factory() as db:
        await db.execute(text(f'SET search_path TO "{test_schema}"'))
        await db.commit()
        async with db.begin():
            # 1. Non-stub player — should be skipped
            non_stub = _make_stub("Non Stub")
            non_stub.is_stub = False
            db.add(non_stub)

            # 2. Stub already enriched — should be skipped
            already_done = _make_stub("Already Done")
            already_done.enrichment_attempted_at = datetime.now(
                timezone.utc
            ).replace(tzinfo=None)
            db.add(already_done)

            # 3. Unenriched stub — should be processed
            eligible = _make_stub("Eligible Player")
            db.add(eligible)

    # Mock external calls: Gemini returns bio, Wikimedia returns None, image gen skipped
    with (
        patch.object(
            player_enrichment_service,
            "_fetch_bio_and_stats",
            new_callable=AsyncMock,
            return_value=_MOCK_BIO,
        ),
        patch.object(
            player_enrichment_service,
            "_find_reference_image",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(
            player_enrichment_service,
            "_generate_portrait",
            new_callable=AsyncMock,
        ),
        patch.object(
            player_enrichment_service,
            "settings",
        ) as mock_settings,
    ):
        mock_settings.gemini_api_key = "fake-key"
        mock_settings.image_gen_size = "1K"

        result = await run_enrichment_sweep(wrapped)

    assert result.players_attempted == 1
    assert result.players_enriched == 1
    assert result.players_failed == 0

    # Verify the eligible player was enriched
    async with wrapped() as db:
        row = (
            await db.execute(
                text(
                    "SELECT school, enrichment_attempted_at FROM players_master"
                    " WHERE display_name = 'Eligible Player'"
                ),
            )
        ).one()
        assert row.school == "Test U"
        assert row.enrichment_attempted_at is not None

    # Verify the other two were untouched
    async with wrapped() as db:
        row = (
            await db.execute(
                text(
                    "SELECT school FROM players_master"
                    " WHERE display_name = 'Non Stub'"
                ),
            )
        ).one()
        assert row.school is None


# ---------------------------------------------------------------------------
# Test 5: Partial failure — bio fails but image succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failure_image_still_saved(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """If Gemini fails but Wikimedia succeeds, reference image is still saved."""
    wrapped = _schema_aware_factory(session_factory, test_schema)

    async with session_factory() as db:
        await db.execute(text(f'SET search_path TO "{test_schema}"'))
        await db.commit()
        async with db.begin():
            player = _make_stub("Partial Fail")
            db.add(player)

    with (
        patch.object(
            player_enrichment_service,
            "_fetch_bio_and_stats",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Gemini down"),
        ),
        patch.object(
            player_enrichment_service,
            "_find_reference_image",
            new_callable=AsyncMock,
            return_value="https://example.com/photo.jpg",
        ),
        patch.object(
            player_enrichment_service,
            "_generate_portrait",
            new_callable=AsyncMock,
        ),
        patch.object(
            player_enrichment_service,
            "settings",
        ) as mock_settings,
    ):
        mock_settings.gemini_api_key = "fake-key"
        mock_settings.image_gen_size = "1K"

        result = await run_enrichment_sweep(wrapped)

    assert result.players_attempted == 1
    assert result.players_enriched == 1

    # Reference image saved, bio untouched, timestamp stamped
    async with wrapped() as db:
        row = (
            await db.execute(
                text(
                    "SELECT school, reference_image_url, enrichment_attempted_at"
                    " FROM players_master WHERE display_name = 'Partial Fail'"
                ),
            )
        ).one()
        assert row.school is None  # bio failed
        assert row.reference_image_url == "https://example.com/photo.jpg"
        assert row.enrichment_attempted_at is not None

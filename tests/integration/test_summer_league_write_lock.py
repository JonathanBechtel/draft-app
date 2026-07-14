"""Integration coverage for Summer League cross-cron writer serialization."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.summer_league.write_lock import (
    acquire_summer_league_writer_lock,
    try_acquire_summer_league_writer_lock,
)


@pytest.mark.asyncio
async def test_writer_lock_blocks_a_second_transaction_in_the_same_schema(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """Desk's blocking lock and ingestion's try-lock share one transaction key."""
    async with session_factory() as desk, session_factory() as ingestion:
        await desk.execute(text(f'SET search_path TO "{test_schema}"'))
        await ingestion.execute(text(f'SET search_path TO "{test_schema}"'))
        await desk.commit()
        await ingestion.commit()
        async with desk.begin():
            await acquire_summer_league_writer_lock(desk)
            async with ingestion.begin():
                assert await try_acquire_summer_league_writer_lock(ingestion) is False

        async with ingestion.begin():
            assert await try_acquire_summer_league_writer_lock(ingestion) is True

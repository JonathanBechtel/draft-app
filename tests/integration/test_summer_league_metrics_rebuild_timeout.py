"""Database coverage for the unlocked Summer League metrics rebuild timeout."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.summer_league.metrics import set_repeatable_read_snapshot


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_metrics_rebuild_disables_idle_timeout_for_its_transaction(
    database_url: str,
    test_schema: str,
    async_engine: object,
) -> None:
    """The repeatable-read rebuild transaction is not killed during Python fitting."""
    _ = async_engine
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
            "server_settings": {"search_path": f'"{test_schema}"'},
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as db:
            async with db.begin():
                await set_repeatable_read_snapshot(db)
                timeout = await db.scalar(
                    text("SHOW idle_in_transaction_session_timeout")
                )
                isolation = await db.scalar(text("SHOW transaction_isolation"))

                assert timeout == "0"
                assert isolation == "repeatable read"
    finally:
        await engine.dispose()

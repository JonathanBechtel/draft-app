"""Transaction-scoped serialization for Summer League projection writers."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# "SLDE" as a signed 32-bit advisory-lock namespace. The first lock key is
# PostgreSQL's hash of current_schema(): production writers share ``public``,
# while pytest-xdist's isolated schemas do not unnecessarily serialize.
_SUMMER_LEAGUE_WRITER_LOCK_KEY = 0x534C4445


async def acquire_summer_league_writer_lock(db: AsyncSession) -> None:
    """Wait for the shared Summer League writer lock for this transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _SUMMER_LEAGUE_WRITER_LOCK_KEY},
    )


async def try_acquire_summer_league_writer_lock(db: AsyncSession) -> bool:
    """Attempt the shared writer lock without delaying lower-priority ingestion."""
    result = await db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _SUMMER_LEAGUE_WRITER_LOCK_KEY},
    )
    return bool(result.scalar_one())

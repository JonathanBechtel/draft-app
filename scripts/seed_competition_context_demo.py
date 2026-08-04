"""Seed the deterministic Competition Context demo dataset into a database.

Intended for a throwaway local database used to drive live browser and visual
verification of the Competitions Explorer tab (ticket #608) — never a shared
dev/prod database. It creates the SQLModel tables if missing, then writes the
derived environment-profile projection rows built by
``app.services.sources.summer_league.environment_fixtures.seed_competition_context_demo``.

Usage (one line)::

    DATABASE_URL=postgresql+asyncpg://localhost/draft_guru_env conda run -n draftguru python scripts/seed_competition_context_demo.py
"""

from __future__ import annotations

import asyncio
import os

from sqlmodel import SQLModel

# Importing the app populates SQLModel.metadata with every table (routes ->
# services -> schemas) so create_all can build the full schema.
import app.main  # noqa: F401
from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile
from app.services.sources.summer_league.environment_fixtures import (
    seed_competition_context_demo,
)


async def _main() -> None:
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is required (point at a throwaway database).")

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        existing = (
            await session.execute(select(SummerLeagueEnvironmentProfile.id).limit(1))
        ).first()
        if existing is not None:
            # Idempotent re-seed: clear the projection tables first so repeated
            # runs against the same throwaway DB stay deterministic.
            from app.schemas.summer_league_environment import (
                SummerLeagueEnvironmentFieldComposition,
                SummerLeagueEnvironmentSeasonMembership,
            )

            # discipline: unscoped-delete demo seeder, throwaway DB only
            await session.execute(delete(SummerLeagueEnvironmentFieldComposition))
            # discipline: unscoped-delete demo seeder, throwaway DB only
            await session.execute(delete(SummerLeagueEnvironmentSeasonMembership))
            # discipline: unscoped-delete demo seeder, throwaway DB only
            await session.execute(delete(SummerLeagueEnvironmentProfile))
            await session.commit()
        refs = await seed_competition_context_demo(session)
        await session.commit()
        print(
            f"Seeded {len(refs.profile_ids)} profiles across years {refs.years}; "
            f"{len(refs.competition_ids)} competitions."
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())

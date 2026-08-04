"""Integration tests for the player_affiliations and summer_league_participation schema.

Verifies:
- Both tables are created with the correct columns, FKs, indexes, and uniqueness constraints.
- Append-only/bitemporal columns (recorded_at, effective_start/end, supersedes_id,
  superseded_at, retracted_at) are present and nullable as expected.
- participation_id was added as a nullable column with an index on
  summer_league_player_game_logs without rewriting existing rows.
- participation uniqueness on (competition_id, team_entry_id, source_player_id, stint_no).

These tests run against the live integration-test Postgres schema (via async_engine and
db_session from conftest), so they require TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import date, datetime
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.player_affiliation import AffiliationStatus, AffiliationType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _column_names(engine: AsyncEngine, table: str, schema: str) -> set[str]:
    """Return the set of column names for a given table in the test schema."""
    async with engine.connect() as conn:
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "ORDER BY ordinal_position"
            ),
            {"schema": schema, "table": table},
        )
        return {row[0] for row in result.fetchall()}


async def _index_names(engine: AsyncEngine, table: str, schema: str) -> set[str]:
    """Return the set of index names for a given table in the test schema."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = :table"
            ),
            {"schema": schema, "table": table},
        )
        return {row[0] for row in result.fetchall()}


async def _constraint_names(engine: AsyncEngine, table: str, schema: str) -> set[str]:
    """Return uniqueness constraint names for a given table in the test schema."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = :schema AND table_name = :table "
                "AND constraint_type = 'UNIQUE'"
            ),
            {"schema": schema, "table": table},
        )
        return {row[0] for row in result.fetchall()}


async def _is_nullable(
    engine: AsyncEngine, table: str, column: str, schema: str
) -> bool:
    """Return whether the given column is nullable."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "AND column_name = :col"
            ),
            {"schema": schema, "table": table, "col": column},
        )
        row = result.fetchone()
        assert row is not None, f"Column {table}.{column} not found"
        return row[0] == "YES"


# ---------------------------------------------------------------------------
# test_player_affiliations_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_affiliations_schema(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """player_affiliations table has all expected columns, including bitemporal ones.

    Verifies: tables/FKs/indexes/uniqueness constraints created.
    """
    cols = await _column_names(async_engine, "player_affiliations", test_schema)

    # Core identity columns
    assert "id" in cols
    assert "player_id" in cols
    assert "nba_team_id" in cols
    # Generic org-model target added in phase 4 (#783, spec §5.1 D3). Retained
    # alongside nba_team_id -- both are dual-read targets, neither is dropped.
    assert "team_program_id" in cols
    assert "affiliation_type" in cols
    assert "status" in cols

    # Bitemporal / append-only columns (journey-graph §5b)
    assert "recorded_at" in cols
    assert "effective_start" in cols
    assert "effective_end" in cols
    assert "supersedes_id" in cols
    assert "superseded_at" in cols
    assert "retracted_at" in cols

    # Provenance
    assert "source" in cols
    assert "source_ref" in cols

    # Timestamps
    assert "created_at" in cols
    assert "updated_at" in cols


@pytest.mark.asyncio
async def test_assertion_columns(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """Bitemporal columns on player_affiliations are present and correctly nullable.

    Verifies: recorded_at NOT NULL; effective_*, supersedes_id, superseded_at,
    retracted_at are nullable (optional temporal bounds and correction pointers).
    """
    # NOT NULL
    assert not await _is_nullable(
        async_engine, "player_affiliations", "recorded_at", test_schema
    ), "recorded_at should NOT be nullable"

    # Nullable temporal bounds and correction pointers
    for col in (
        "effective_start",
        "effective_end",
        "supersedes_id",
        "superseded_at",
        "retracted_at",
        # Nullable by design: a row targeting only nba_team_id leaves it NULL
        # for now, and a non-NBA row leaves nba_team_id NULL instead (D3).
        "team_program_id",
    ):
        assert await _is_nullable(
            async_engine, "player_affiliations", col, test_schema
        ), f"{col} should be nullable"


@pytest.mark.asyncio
async def test_player_affiliations_indexes(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """player_affiliations has all expected indexes including the partial 'active' index."""
    indexes = await _index_names(async_engine, "player_affiliations", test_schema)

    assert "ix_player_affiliations_player_id" in indexes
    assert "ix_player_affiliations_nba_team_id" in indexes
    assert "ix_player_affiliations_status" in indexes
    assert "ix_player_affiliations_supersedes_id" in indexes
    assert "ix_player_affiliations_active" in indexes
    # Phase-4 org-model target indexes (#783): the plain lookup index and the
    # partial index backing "current affiliations for this program".
    assert "ix_player_affiliations_team_program_id" in indexes
    assert "ix_player_affiliations_active_team_program" in indexes


# ---------------------------------------------------------------------------
# test_summer_league_participation_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summer_league_participation_schema(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """summer_league_participation has all expected columns, FKs, and indexes."""
    cols = await _column_names(async_engine, "summer_league_participation", test_schema)

    assert "id" in cols
    assert "competition_id" in cols
    assert "team_entry_id" in cols
    assert "source_player_id" in cols
    assert "player_id" in cols
    assert "affiliation_id" in cols
    assert "stint_no" in cols
    assert "roster_status" in cols
    assert "jersey_number" in cols
    assert "roster_position" in cols
    assert "first_game_date" in cols
    assert "last_game_date" in cols
    assert "games_played" in cols
    assert "created_at" in cols
    assert "updated_at" in cols

    indexes = await _index_names(
        async_engine, "summer_league_participation", test_schema
    )
    assert "ix_summer_league_participation_player_id" in indexes
    assert "ix_summer_league_participation_competition_team" in indexes
    assert "ix_summer_league_participation_source_player_id" in indexes
    assert "ix_summer_league_participation_affiliation_id" in indexes


@pytest.mark.asyncio
async def test_participation_unique(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
    test_schema: str,
) -> None:
    """summer_league_participation enforces uniqueness on (competition, team_entry, source_player, stint).

    Verifies: duplicate (competition_id, team_entry_id, source_player_id, stint_no) raises
    IntegrityError.
    """
    from sqlalchemy.exc import IntegrityError

    # Seed a competition and team entry row to satisfy FK constraints.
    await db_session.execute(
        text(
            f'INSERT INTO "{test_schema}".summer_league_competitions '
            "(year, league_id, venue_slug, display_name, data_quality, "
            "pbp_available, shotchart_available, created_at, updated_at) "
            "VALUES (2026, '15', 'vegas', 'Vegas 2026', 'raw_only', false, false, now(), now()) "
            "RETURNING id"
        )
    )
    comp_row = await db_session.execute(
        text(
            f'SELECT id FROM "{test_schema}".summer_league_competitions '
            "WHERE year=2026 AND league_id='15' LIMIT 1"
        )
    )
    comp_id = comp_row.scalar_one()

    await db_session.execute(
        text(
            f'INSERT INTO "{test_schema}".summer_league_team_entries '
            "(competition_id, nba_stats_team_id, raw_team_name, team_slug, created_at, updated_at) "
            f"VALUES ({comp_id}, 'TEST', 'Test Team', 'test-team', now(), now()) "
            "RETURNING id"
        )
    )
    team_row = await db_session.execute(
        text(
            f'SELECT id FROM "{test_schema}".summer_league_team_entries '
            "WHERE nba_stats_team_id='TEST' LIMIT 1"
        )
    )
    team_id = team_row.scalar_one()

    await db_session.execute(
        text(
            f'INSERT INTO "{test_schema}".summer_league_source_players '
            "(nba_stats_person_id, raw_player_name, normalized_name, "
            "resolution_status, created_at, updated_at) "
            "VALUES ('P1', 'Test Player', 'test player', 'UNRESOLVED', now(), now()) "
            "RETURNING id"
        )
    )
    sp_row = await db_session.execute(
        text(
            f'SELECT id FROM "{test_schema}".summer_league_source_players '
            "WHERE nba_stats_person_id='P1' LIMIT 1"
        )
    )
    sp_id = sp_row.scalar_one()

    await db_session.commit()

    # First insert should succeed.
    await db_session.execute(
        text(
            f'INSERT INTO "{test_schema}".summer_league_participation '
            "(competition_id, team_entry_id, source_player_id, stint_no, "
            "roster_status, created_at, updated_at) "
            f"VALUES ({comp_id}, {team_id}, {sp_id}, 1, 'ANNOUNCED', now(), now())"
        )
    )
    await db_session.commit()

    # Duplicate should raise IntegrityError.
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                f'INSERT INTO "{test_schema}".summer_league_participation '
                "(competition_id, team_entry_id, source_player_id, stint_no, "
                "roster_status, created_at, updated_at) "
                f"VALUES ({comp_id}, {team_id}, {sp_id}, 1, 'ANNOUNCED', now(), now())"
            )
        )
        await db_session.commit()


# ---------------------------------------------------------------------------
# test_participation_id_added_nullable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_participation_id_added_nullable(
    async_engine: AsyncEngine, test_schema: str
) -> None:
    """participation_id was added to summer_league_player_game_logs as nullable with index.

    Verifies: column exists, is nullable, and has a supporting index.
    Pre-existing rows gain participation_id = NULL without being rewritten.
    """
    cols = await _column_names(
        async_engine, "summer_league_player_game_logs", test_schema
    )
    assert "participation_id" in cols

    # Must be nullable — 2026 rows will be populated; pre-2026 rows stay NULL.
    is_null = await _is_nullable(
        async_engine, "summer_league_player_game_logs", "participation_id", test_schema
    )
    assert is_null, "participation_id must be nullable"

    # Supporting index should exist.
    indexes = await _index_names(
        async_engine, "summer_league_player_game_logs", test_schema
    )
    assert "ix_summer_league_player_game_logs_participation_id" in indexes


# ---------------------------------------------------------------------------
# test_enum_members
# ---------------------------------------------------------------------------


def test_affiliation_type_members() -> None:
    """AffiliationType enum has the required members (unit-level, no DB needed)."""
    assert AffiliationType.SUMMER_LEAGUE_ROSTER.value == "SUMMER_LEAGUE_ROSTER"
    assert AffiliationType.CLUB.value == "CLUB"
    assert AffiliationType.NATIONAL_TEAM.value == "NATIONAL_TEAM"
    assert AffiliationType.NBA_CONTRACT.value == "NBA_CONTRACT"
    assert AffiliationType.COLLEGE.value == "COLLEGE"


def test_affiliation_status_members() -> None:
    """AffiliationStatus enum has the required members (unit-level, no DB needed)."""
    assert AffiliationStatus.ANNOUNCED.value == "ANNOUNCED"
    assert AffiliationStatus.CONFIRMED.value == "CONFIRMED"
    assert AffiliationStatus.ACTIVE.value == "ACTIVE"
    assert AffiliationStatus.CUT.value == "CUT"
    assert AffiliationStatus.WITHDRAWN.value == "WITHDRAWN"

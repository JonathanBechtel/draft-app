"""Integration tests for the player_embeddings table.

Verifies that the schema and migration land correctly: rows can be
inserted and retrieved through the SQLModel class.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import make_player


@pytest.mark.asyncio
async def test_insert_and_select_player_embedding(db_session: AsyncSession) -> None:
    """Insert a player embedding row and retrieve it by player_id.

    Verifies the FK relationship, vector column, and model_name field
    round-trip correctly through the ORM.
    """
    from app.schemas.player_embeddings import PlayerEmbedding
    from app.schemas.players_master import PlayerMaster

    # Create a parent player row.
    player = make_player("Victor", "Wembanyama")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    # Build a 768-dim unit vector (all zeros except first element).
    embedding_vector = [0.0] * 768
    embedding_vector[0] = 1.0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = PlayerEmbedding(
        player_id=player.id,
        embedding=embedding_vector,
        model_name="text-embedding-004",
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()

    # Retrieve via raw SQL to confirm the row was stored.
    result = await db_session.execute(
        text(
            "SELECT player_id, model_name, embedding::text "
            "FROM player_embeddings WHERE player_id = :pid"
        ),
        {"pid": player.id},
    )
    fetched = result.fetchone()
    assert fetched is not None
    assert fetched.player_id == player.id
    assert fetched.model_name == "text-embedding-004"
    # The embedding stored as text should start with '[' (pgvector text format).
    assert fetched[2].startswith("[")


@pytest.mark.asyncio
async def test_player_embedding_cascade_delete(db_session: AsyncSession) -> None:
    """Deleting a player should cascade-delete the embedding row.

    Verifies the ON DELETE CASCADE FK constraint is active.
    """
    from app.schemas.player_embeddings import PlayerEmbedding

    player = make_player("Chet", "Holmgren")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    embedding_vector = [0.1] * 768
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = PlayerEmbedding(
        player_id=player.id,
        embedding=embedding_vector,
        model_name="text-embedding-004",
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()

    # Delete the player — the embedding should cascade away.
    await db_session.delete(player)
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT COUNT(*) FROM player_embeddings WHERE player_id = :pid"),
        {"pid": player.id},
    )
    count = result.scalar()
    assert count == 0, "Embedding row should be deleted when player is deleted"

"""Add pgvector extension and player_embeddings table.

Revision ID: v1w2x3y4z5a6
Revises: u0v1w2x3y4z5
Create Date: 2026-05-26

Adds the pgvector extension and a ``player_embeddings`` table that stores
768-dimensional Gemini text-embedding-004 vectors for each player.  The
table is the storage layer for the vector-search entity-resolution path.

This migration:
  - Creates the pgvector extension (CREATE EXTENSION IF NOT EXISTS vector).
  - Creates the player_embeddings table with:
    - player_id: PK + FK to players_master.id (CASCADE on delete)
    - embedding: vector(768) NOT NULL
    - model_name: text NOT NULL (tracks which model produced the vector)
    - created_at, updated_at: timestamps
  - Creates an HNSW index on embedding using vector_cosine_ops for fast
    k-NN search.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "v1w2x3y4z5a6"
down_revision: Union[str, None] = "u0v1w2x3y4z5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create pgvector extension and player_embeddings table with HNSW index."""
    # 1. Enable pgvector extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. Create the player_embeddings table.
    #    The embedding column is initially TEXT; we cast it to vector(768) once
    #    the extension is confirmed active.  This avoids alembic needing to know
    #    about the pgvector type at DDL-generation time.
    op.create_table(
        "player_embeddings",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players_master.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("player_id"),
    )

    # 3. Cast embedding column to the real vector(768) type now that the
    #    extension is installed.
    op.execute(
        "ALTER TABLE player_embeddings "
        "ALTER COLUMN embedding TYPE vector(768) USING embedding::vector(768)"
    )

    # 4. HNSW index for fast cosine-distance k-NN queries.
    op.execute(
        "CREATE INDEX ix_player_embeddings_hnsw "
        "ON player_embeddings USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Drop HNSW index, player_embeddings table, and pgvector extension."""
    op.execute("DROP INDEX IF EXISTS ix_player_embeddings_hnsw")
    op.drop_table("player_embeddings")
    op.execute("DROP EXTENSION IF EXISTS vector")

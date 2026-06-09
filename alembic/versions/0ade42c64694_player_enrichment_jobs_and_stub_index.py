"""Add PlayerEnrichmentJob table and is_stub index on players_master.

Revision ID: 0ade42c64694
Revises: z5a6b7c8d9e0
Create Date: 2026-06-06

New table ``player_enrichment_jobs`` tracks on-demand enrichment requests,
mirroring the ``ImageBatchJob`` state/index shape. Also adds a composite
index on ``players_master(is_stub, created_at)`` to back the Stubs admin
tab list query (``WHERE is_stub = true ORDER BY created_at DESC``).

- New table → created via ``SQLModel.metadata.create_all`` per repo convention.
- Existing table index → added via ``op.create_index`` (no drop/recreate of
  ``players_master``).
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.player_enrichment_jobs import PlayerEnrichmentJob

# revision identifiers, used by Alembic.
revision: str = "0ade42c64694"
down_revision: Union[str, None] = "z5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the new player_enrichment_jobs table (and its indexes) wholesale.
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[PlayerEnrichmentJob.__table__],  # type: ignore[attr-defined]
    )

    # Add the partial-style composite index on the existing players_master table.
    # Backs: WHERE is_stub = true ORDER BY created_at DESC
    op.create_index(
        "ix_players_master_is_stub_created",
        "players_master",
        ["is_stub", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_players_master_is_stub_created", table_name="players_master")

    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[PlayerEnrichmentJob.__table__],  # type: ignore[attr-defined]
    )

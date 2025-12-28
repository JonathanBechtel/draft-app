"""add reference_image_url to players_master

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2025-12-27
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
import sqlmodel

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "players_master",
        sa.Column(
            "reference_image_url",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("players_master", "reference_image_url")

"""add reference_image_s3_key to player_master

Revision ID: bc8962e9d4d3
Revises: bc9443ccd2b6
Create Date: 2026-03-28 22:30:11.804683
"""
from alembic import op  # type: ignore[attr-defined]
import sqlmodel.sql.sqltypes
import sqlalchemy as sa

revision = 'bc8962e9d4d3'
down_revision = 'bc9443ccd2b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'players_master',
        sa.Column('reference_image_s3_key', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('players_master', 'reference_image_s3_key')

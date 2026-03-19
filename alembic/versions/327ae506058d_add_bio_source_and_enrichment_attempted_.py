"""add bio_source and enrichment_attempted_at to players_master

Revision ID: 327ae506058d
Revises: f530b9cc9a0b
Create Date: 2026-03-19 00:07:17.987940
"""
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy import text
import sqlmodel.sql.sqltypes

revision = '327ae506058d'
down_revision = 'f530b9cc9a0b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Add bio_source (idempotent: AUTO_INIT_DB may have added it)
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'players_master' AND column_name = 'bio_source'"
    ))
    if not result.fetchone():
        op.add_column(
            'players_master',
            sa.Column('bio_source', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )

    # Add enrichment_attempted_at (idempotent: AUTO_INIT_DB may have added it)
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'players_master' AND column_name = 'enrichment_attempted_at'"
    ))
    if not result.fetchone():
        op.add_column(
            'players_master',
            sa.Column('enrichment_attempted_at', sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column('players_master', 'enrichment_attempted_at')
    op.drop_column('players_master', 'bio_source')

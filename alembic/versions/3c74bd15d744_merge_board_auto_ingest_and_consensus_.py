"""merge board auto-ingest and consensus-alignment heads

Revision ID: 3c74bd15d744
Revises: x3y4z5a6b7c8, y4z5a6b7c8d9
Create Date: 2026-05-31 14:48:56.943892
"""
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa

revision = '3c74bd15d744'
down_revision = ('x3y4z5a6b7c8', 'y4z5a6b7c8d9')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

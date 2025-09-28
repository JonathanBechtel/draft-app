"""Initial schema for players table.

Revision ID: 20240924_000001
Revises:
Create Date: 2024-09-24 00:00:01
"""
from alembic import op
from sqlmodel import SQLModel

from app.schemas.players import PlayerTable

revision = "20240924_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.create_all(bind=bind, tables=[PlayerTable.__table__])


def downgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.drop_all(bind=bind, tables=[PlayerTable.__table__])
    player_position_enum = PlayerTable.__table__.c.player_position.type
    drop_enum = getattr(player_position_enum, "drop", None)
    if callable(drop_enum):
        drop_enum(bind, checkfirst=True)

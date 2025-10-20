"""Initial schema for players table.

Revision ID: 20240924_000001
Revises:
Create Date: 2024-09-24 00:00:01
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20240924_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Store the enum names (g, f, c) as used by SAEnum(Position) defaults.
    player_position_enum = postgresql.ENUM(
        "g",
        "f",
        "c",
        name="player_position_enum",
        create_type=False,
    )
    player_position_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "players",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("player_position", player_position_enum, nullable=False),
        sa.Column("school", sa.String(), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_players_deleted_at", "players", ["deleted_at"], unique=False)


def downgrade() -> None:
    # In case the index was already removed, drop with IF EXISTS to tolerate drift.
    op.execute(sa.text("DROP INDEX IF EXISTS ix_players_deleted_at"))
    op.drop_table("players")
    player_position_enum = postgresql.ENUM(
        "g",
        "f",
        "c",
        name="player_position_enum",
        create_type=False,
    )
    player_position_enum.drop(op.get_bind(), checkfirst=True)

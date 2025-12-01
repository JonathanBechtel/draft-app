"""Remove players table

Revision ID: 9ce336751346
Revises: f1a2b3c4d5e6
Create Date: 2025-12-01 01:07:18.753755
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "9ce336751346"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop legacy/demo tables defensively; tolerate missing objects.
    tables_to_drop = [
        "consensus_mocks",
        "consensusmock",
        "news_feed",
        "newsitem",
        "player_stock_history",
        "playerstock",
        "players",
    ]
    for table in tables_to_drop:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

    # Drop the enum type used by players.player_position
    player_position_enum = postgresql.ENUM(
        "g",
        "f",
        "c",
        name="player_position_enum",
        create_type=False,
    )
    player_position_enum.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Recreate the legacy players table and enum (minimal downgrade).
    player_position_enum = postgresql.ENUM(
        "g",
        "f",
        "c",
        name="player_position_enum",
        create_type=True,
    )
    player_position_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "players",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("name", sa.VARCHAR(), nullable=False),
        sa.Column("player_position", player_position_enum, nullable=False),
        sa.Column("school", sa.VARCHAR(), nullable=False),
        sa.Column("birth_date", sa.DATE(), nullable=False),
        sa.Column("deleted_at", postgresql.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("players_pkey")),
    )
    op.create_index(
        op.f("ix_players_deleted_at"), "players", ["deleted_at"], unique=False
    )

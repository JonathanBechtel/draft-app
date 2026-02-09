"""Add player mentions junction table and is_stub column.

Revision ID: h7i8j9k0l1m2
Revises: g6h7i8j9k0l1
Create Date: 2026-02-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "h7i8j9k0l1m2"
down_revision: Union[str, None] = "g6h7i8j9k0l1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add is_stub column to players_master
    op.add_column(
        "players_master",
        sa.Column("is_stub", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # Create news_item_player_mentions junction table
    op.create_table(
        "news_item_player_mentions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("news_item_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["news_item_id"], ["news_items.id"], name="fk_mentions_news_item"),
        sa.ForeignKeyConstraint(["player_id"], ["players_master.id"], name="fk_mentions_player"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("news_item_id", "player_id", name="uq_news_item_player_mention"),
    )

    # Create indexes
    op.create_index("ix_news_item_player_mentions_news_item_id", "news_item_player_mentions", ["news_item_id"])
    op.create_index("ix_news_item_player_mentions_player_id", "news_item_player_mentions", ["player_id"])
    op.create_index("ix_mentions_player_created", "news_item_player_mentions", ["player_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_mentions_player_created", table_name="news_item_player_mentions")
    op.drop_index("ix_news_item_player_mentions_player_id", table_name="news_item_player_mentions")
    op.drop_index("ix_news_item_player_mentions_news_item_id", table_name="news_item_player_mentions")
    op.drop_table("news_item_player_mentions")
    op.drop_column("players_master", "is_stub")

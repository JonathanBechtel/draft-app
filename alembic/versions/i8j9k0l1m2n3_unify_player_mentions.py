"""Unify player mentions into player_content_mentions.

Revision ID: i8j9k0l1m2n3
Revises: h7i8j9k0l1m2
Create Date: 2026-02-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "i8j9k0l1m2n3"
down_revision: Union[str, None] = "h7i8j9k0l1m2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create unified player_content_mentions table
    op.create_table(
        "player_content_mentions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("content_id", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players_master.id"], name="fk_pcm_player"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_type", "content_id", "player_id", name="uq_content_mention"
        ),
    )

    # Create indexes
    op.create_index(
        "ix_player_content_mentions_player_id",
        "player_content_mentions",
        ["player_id"],
    )
    op.create_index(
        "ix_pcm_player_created",
        "player_content_mentions",
        ["player_id", "created_at"],
    )
    op.create_index(
        "ix_pcm_content_lookup",
        "player_content_mentions",
        ["content_type", "content_id"],
    )

    # 2. Copy data from old table, joining news_items for published_at
    op.execute(
        """
        INSERT INTO player_content_mentions
            (player_id, content_type, content_id, published_at, source, created_at)
        SELECT
            m.player_id,
            'news',
            m.news_item_id,
            n.published_at,
            m.source,
            m.created_at
        FROM news_item_player_mentions m
        LEFT JOIN news_items n ON n.id = m.news_item_id
        """
    )

    # 3. Drop old table and its indexes
    op.drop_index(
        "ix_mentions_player_created", table_name="news_item_player_mentions"
    )
    op.drop_index(
        "ix_news_item_player_mentions_player_id",
        table_name="news_item_player_mentions",
    )
    op.drop_index(
        "ix_news_item_player_mentions_news_item_id",
        table_name="news_item_player_mentions",
    )
    op.drop_table("news_item_player_mentions")


def downgrade() -> None:
    # 1. Recreate old table
    op.create_table(
        "news_item_player_mentions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("news_item_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["news_item_id"], ["news_items.id"], name="fk_mentions_news_item"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players_master.id"], name="fk_mentions_player"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "news_item_id", "player_id", name="uq_news_item_player_mention"
        ),
    )
    op.create_index(
        "ix_news_item_player_mentions_news_item_id",
        "news_item_player_mentions",
        ["news_item_id"],
    )
    op.create_index(
        "ix_news_item_player_mentions_player_id",
        "news_item_player_mentions",
        ["player_id"],
    )
    op.create_index(
        "ix_mentions_player_created",
        "news_item_player_mentions",
        ["player_id", "created_at"],
    )

    # 2. Copy news-type data back
    op.execute(
        """
        INSERT INTO news_item_player_mentions
            (player_id, news_item_id, source, created_at)
        SELECT
            player_id,
            content_id,
            source,
            created_at
        FROM player_content_mentions
        WHERE content_type = 'news'
        """
    )

    # 3. Drop unified table
    op.drop_index(
        "ix_pcm_content_lookup", table_name="player_content_mentions"
    )
    op.drop_index(
        "ix_pcm_player_created", table_name="player_content_mentions"
    )
    op.drop_index(
        "ix_player_content_mentions_player_id",
        table_name="player_content_mentions",
    )
    op.drop_table("player_content_mentions")

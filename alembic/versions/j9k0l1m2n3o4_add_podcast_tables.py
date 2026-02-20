"""Add podcast_shows and podcast_episodes tables.

Revision ID: j9k0l1m2n3o4
Revises: i8j9k0l1m2n3
Create Date: 2026-02-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "j9k0l1m2n3o4"
down_revision: Union[str, None] = "i8j9k0l1m2n3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create podcast_shows table
    op.create_table(
        "podcast_shows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("feed_url", sa.String(), nullable=False),
        sa.Column("artwork_url", sa.String(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("website_url", sa.String(), nullable=True),
        sa.Column(
            "is_draft_focused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("fetch_interval_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_url", name="uq_podcast_shows_feed_url"),
    )
    op.create_index("ix_podcast_shows_name", "podcast_shows", ["name"])
    op.create_index("ix_podcast_shows_is_active", "podcast_shows", ["is_active"])

    # Create podcast_episodes table
    op.create_table(
        "podcast_episodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("show_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("audio_url", sa.String(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("episode_url", sa.String(), nullable=True),
        sa.Column("artwork_url", sa.String(), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode_number", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("tag", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["show_id"], ["podcast_shows.id"], name="fk_podcast_episodes_show"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players_master.id"], name="fk_podcast_episodes_player"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "show_id", "external_id", name="uq_podcast_episodes_show_external"
        ),
    )
    op.create_index("ix_podcast_episodes_show_id", "podcast_episodes", ["show_id"])
    op.create_index(
        "ix_podcast_episodes_external_id", "podcast_episodes", ["external_id"]
    )
    op.create_index(
        "ix_podcast_episodes_published_at", "podcast_episodes", ["published_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_podcast_episodes_published_at", table_name="podcast_episodes")
    op.drop_index("ix_podcast_episodes_external_id", table_name="podcast_episodes")
    op.drop_index("ix_podcast_episodes_show_id", table_name="podcast_episodes")
    op.drop_table("podcast_episodes")

    op.drop_index("ix_podcast_shows_is_active", table_name="podcast_shows")
    op.drop_index("ix_podcast_shows_name", table_name="podcast_shows")
    op.drop_table("podcast_shows")

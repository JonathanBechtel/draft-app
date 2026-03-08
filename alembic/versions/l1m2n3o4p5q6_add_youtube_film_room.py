"""Add YouTube film-room tables and VIDEO content type enum value.

Revision ID: l1m2n3o4p5q6
Revises: k0l1m2n3o4p5
Create Date: 2026-03-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "l1m2n3o4p5q6"
down_revision: Union[str, None] = "k0l1m2n3o4p5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE contenttype ADD VALUE IF NOT EXISTS 'VIDEO'")

    bind = op.get_bind()
    youtube_video_tag_enum = postgresql.ENUM(
        "THINK_PIECE",
        "CONVERSATION",
        "SCOUTING_REPORT",
        "HIGHLIGHTS",
        "MONTAGE",
        name="youtubevideotag",
        create_type=False,
    )
    youtube_video_tag_enum.create(bind, checkfirst=True)

    op.create_table(
        "youtube_channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("channel_url", sa.String(), nullable=True),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("uploads_playlist_id", sa.String(), nullable=True),
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
        sa.Column(
            "fetch_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", name="uq_youtube_channels_channel_id"),
    )
    op.create_index("ix_youtube_channels_name", "youtube_channels", ["name"])
    op.create_index(
        "ix_youtube_channels_is_active",
        "youtube_channels",
        ["is_active"],
    )

    op.create_table(
        "youtube_videos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("youtube_url", sa.String(), nullable=False),
        sa.Column("thumbnail_url", sa.String(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column(
            "tag",
            youtube_video_tag_enum,
            nullable=False,
            server_default="SCOUTING_REPORT",
        ),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column(
            "is_manually_added",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0"),
        sa.CheckConstraint("view_count IS NULL OR view_count >= 0"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["youtube_channels.id"],
            name="fk_youtube_videos_channel",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_youtube_videos_external_id",
        "youtube_videos",
        ["external_id"],
        unique=True,
    )
    op.create_index("ix_youtube_videos_published_at", "youtube_videos", ["published_at"])
    op.create_index(
        "ix_youtube_videos_channel_published",
        "youtube_videos",
        ["channel_id", "published_at"],
    )
    op.create_index(
        "ix_youtube_videos_tag_published",
        "youtube_videos",
        ["tag", "published_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_youtube_videos_tag_published", table_name="youtube_videos")
    op.drop_index("ix_youtube_videos_channel_published", table_name="youtube_videos")
    op.drop_index("ix_youtube_videos_published_at", table_name="youtube_videos")
    op.drop_index("ix_youtube_videos_external_id", table_name="youtube_videos")
    op.drop_table("youtube_videos")

    op.drop_index("ix_youtube_channels_is_active", table_name="youtube_channels")
    op.drop_index("ix_youtube_channels_name", table_name="youtube_channels")
    op.drop_table("youtube_channels")

    op.execute("DROP TYPE IF EXISTS youtubevideotag")

    # Intentionally do not remove VIDEO from contenttype in downgrade.
    # PostgreSQL enum label removal is non-trivial and can be unsafe on live data.

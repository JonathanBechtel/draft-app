"""add pending_image_previews table for preview/accept flow

Revision ID: e4f5g6h7i8j9
Revises: d8e1f2a3b4c5
Create Date: 2026-02-01
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
import sqlmodel

revision = "e4f5g6h7i8j9"
down_revision = "d8e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create pending_image_previews table
    op.create_table(
        "pending_image_previews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("source_asset_id", sa.Integer(), nullable=True),
        sa.Column("style", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("image_data_base64", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("likeness_description", sa.Text(), nullable=True),
        sa.Column(
            "used_likeness_ref", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("reference_image_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("generation_time_sec", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players_master.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id"],
            ["player_image_assets.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index(
        "ix_pending_previews_player",
        "pending_image_previews",
        ["player_id"],
        unique=False,
    )
    op.create_index(
        "ix_pending_previews_expires",
        "pending_image_previews",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pending_previews_expires", table_name="pending_image_previews")
    op.drop_index("ix_pending_previews_player", table_name="pending_image_previews")
    op.drop_table("pending_image_previews")

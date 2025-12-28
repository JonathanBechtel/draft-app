"""add image snapshot tables

Revision ID: a1b2c3d4e5f6
Revises: 5f7a0a9c1b2d
Create Date: 2025-12-27
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel

revision = "a1b2c3d4e5f6"
down_revision = "5f7a0a9c1b2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cohort_type_enum = postgresql.ENUM(
        "current_draft",
        "all_time_draft",
        "current_nba",
        "all_time_nba",
        "global_scope",
        name="cohort_type_enum",
        create_type=False,
    )

    # Create player_image_snapshots table
    op.create_table(
        "player_image_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("style", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "cohort",
            cohort_type_enum,
            nullable=False,
        ),
        sa.Column("draft_year", sa.Integer(), nullable=True),
        sa.Column("population_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("image_size", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "system_prompt_version", sqlmodel.sql.sqltypes.AutoString(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "style",
            "cohort",
            "run_key",
            "version",
            name="uq_image_snapshots_style_cohort_run_ver",
        ),
    )
    op.create_index(
        op.f("ix_player_image_snapshots_run_key"),
        "player_image_snapshots",
        ["run_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_image_snapshots_style"),
        "player_image_snapshots",
        ["style"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_image_snapshots_draft_year"),
        "player_image_snapshots",
        ["draft_year"],
        unique=False,
    )
    # Partial unique index for is_current
    op.create_index(
        "uq_image_snapshots_current",
        "player_image_snapshots",
        ["style", "cohort", sa.text("coalesce(draft_year, -1)")],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
    )

    # Create player_image_assets table
    op.create_table(
        "player_image_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("s3_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("s3_bucket", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("public_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("generation_time_sec", sa.Float(), nullable=True),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("likeness_description", sa.Text(), nullable=True),
        sa.Column(
            "used_likeness_ref", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "reference_image_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["player_image_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players_master.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "player_id",
            name="uq_image_asset_snapshot_player",
        ),
    )
    op.create_index(
        "ix_image_assets_player",
        "player_image_assets",
        ["player_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_assets_snapshot",
        "player_image_assets",
        ["snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_image_assets_snapshot", table_name="player_image_assets")
    op.drop_index("ix_image_assets_player", table_name="player_image_assets")
    op.drop_table("player_image_assets")

    op.drop_index(
        "uq_image_snapshots_current",
        table_name="player_image_snapshots",
        postgresql_where=sa.text("is_current = true"),
    )
    op.drop_index(
        op.f("ix_player_image_snapshots_draft_year"),
        table_name="player_image_snapshots",
    )
    op.drop_index(
        op.f("ix_player_image_snapshots_style"), table_name="player_image_snapshots"
    )
    op.drop_index(
        op.f("ix_player_image_snapshots_run_key"), table_name="player_image_snapshots"
    )
    op.drop_table("player_image_snapshots")

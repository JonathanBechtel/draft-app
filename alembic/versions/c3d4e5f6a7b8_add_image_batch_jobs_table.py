"""add image_batch_jobs table for batch processing

Revision ID: c3d4e5f6a7b8
Revises: 95dcddec45ad
Create Date: 2026-01-02
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel

revision = "c3d4e5f6a7b8"
down_revision = "95dcddec45ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the batch_job_state_enum type explicitly with checkfirst
    batch_job_state_enum = postgresql.ENUM(
        "JOB_STATE_PENDING",
        "JOB_STATE_RUNNING",
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        name="batch_job_state_enum",
        create_type=False,  # Don't auto-create in op.create_table; we create explicitly below
    )
    batch_job_state_enum.create(op.get_bind(), checkfirst=True)

    # Create image_batch_jobs table
    op.create_table(
        "image_batch_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "gemini_job_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column(
            "state",
            batch_job_state_enum,
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("player_ids_json", sa.Text(), nullable=False),
        sa.Column("style", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("image_size", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "fetch_likeness", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("total_requests", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["player_image_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index(
        op.f("ix_image_batch_jobs_gemini_job_name"),
        "image_batch_jobs",
        ["gemini_job_name"],
        unique=True,
    )
    op.create_index(
        "ix_batch_jobs_state",
        "image_batch_jobs",
        ["state"],
        unique=False,
    )
    op.create_index(
        "ix_batch_jobs_snapshot",
        "image_batch_jobs",
        ["snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_batch_jobs_snapshot", table_name="image_batch_jobs")
    op.drop_index("ix_batch_jobs_state", table_name="image_batch_jobs")
    op.drop_index(
        op.f("ix_image_batch_jobs_gemini_job_name"), table_name="image_batch_jobs"
    )
    op.drop_table("image_batch_jobs")

    # Drop the enum type
    batch_job_state_enum = postgresql.ENUM(
        "JOB_STATE_PENDING",
        "JOB_STATE_RUNNING",
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        name="batch_job_state_enum",
    )
    batch_job_state_enum.drop(op.get_bind(), checkfirst=True)

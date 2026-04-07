"""add school_raw to players_master and espn_id to college_schools

Revision ID: 35d4c25324b3
Revises: b9705695210a
Create Date: 2026-04-06
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa

revision = "35d4c25324b3"
down_revision = "b9705695210a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add school_raw column to preserve original values before canonicalization
    op.add_column(
        "players_master",
        sa.Column("school_raw", sa.String(), nullable=True),
    )

    # Copy current school values into school_raw
    op.execute("UPDATE players_master SET school_raw = school")

    # Add espn_id to college_schools for logo retrieval
    op.add_column(
        "college_schools",
        sa.Column("espn_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("college_schools", "espn_id")
    op.drop_column("players_master", "school_raw")

"""add combine_score enum values

Revision ID: bc9443ccd2b6
Revises: 8ede11b10150
Create Date: 2026-03-28
"""

from alembic import op  # type: ignore[attr-defined]


revision = "bc9443ccd2b6"
down_revision = "8ede11b10150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE metric_source_enum ADD VALUE IF NOT EXISTS 'combine_score'"
        )
        op.execute(
            "ALTER TYPE snapshot_source_enum ADD VALUE IF NOT EXISTS 'combine_score'"
        )
        op.execute(
            "ALTER TYPE metric_category_enum ADD VALUE IF NOT EXISTS 'combine_overall'"
        )


def downgrade() -> None:
    # Postgres does not support removing enum values; the values are harmless if unused.
    pass

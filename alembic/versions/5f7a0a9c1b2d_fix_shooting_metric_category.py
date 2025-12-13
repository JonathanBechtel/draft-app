"""fix shooting metric_definitions category

Revision ID: 5f7a0a9c1b2d
Revises: 2c1f5a9d3b7e
Create Date: 2025-12-13
"""

from alembic import op  # type: ignore[attr-defined]


revision = "5f7a0a9c1b2d"
down_revision = "2c1f5a9d3b7e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure the enum includes the 'shooting' category before updating rows.
    # Postgres requires the new enum value to be committed before it can be used,
    # so we run the ALTER TYPE in an autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE metric_category_enum ADD VALUE IF NOT EXISTS 'shooting'")
    op.execute(
        """
        UPDATE metric_definitions
        SET category = 'shooting'
        WHERE source = 'combine_shooting'
          AND category = 'combine_performance'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE metric_definitions
        SET category = 'combine_performance'
        WHERE source = 'combine_shooting'
          AND category = 'shooting'
        """
    )

"""add unique constraint on s3_key to player_image_assets

This migration:
1. Cleans up existing duplicate s3_key records (keeps the most recent)
2. Adds a unique constraint to prevent future duplicates

Revision ID: f5g6h7i8j9k0
Revises: e4f5g6h7i8j9
Create Date: 2026-02-01
"""

from alembic import op  # type: ignore[attr-defined]

revision = "f5g6h7i8j9k0"
down_revision = "e4f5g6h7i8j9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Clean up existing duplicates (keep the record with the highest id)
    # This handles cases like Matas Buzelis having two records for the same s3_key
    op.execute("""
        DELETE FROM player_image_assets a
        USING player_image_assets b
        WHERE a.s3_key = b.s3_key
          AND a.id < b.id
    """)

    # Step 2: Add unique constraint on s3_key
    # This enforces one image per player per style (since s3_key encodes player+style)
    op.create_unique_constraint(
        "uq_image_asset_s3_key",
        "player_image_assets",
        ["s3_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_image_asset_s3_key", "player_image_assets", type_="unique")

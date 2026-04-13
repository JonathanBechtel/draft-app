"""Merge player lifecycle and reference-image migration heads.

Revision ID: 7c8d9e0f1a2b
Revises: 6a7b8c9d0e1f, bc8962e9d4d3
Create Date: 2026-04-06
"""

revision = "7c8d9e0f1a2b"
down_revision = ("6a7b8c9d0e1f", "bc8962e9d4d3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge divergent migration heads without additional schema changes."""


def downgrade() -> None:
    """Unmerge divergent migration heads without additional schema changes."""

"""Merge team-table and lifecycle migration heads.

Revision ID: d1e2f3a4b5c6
Revises: 7c8d9e0f1a2b, b9705695210a
Create Date: 2026-04-06
"""

revision = "d1e2f3a4b5c6"
down_revision = ("7c8d9e0f1a2b", "b9705695210a")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge divergent migration heads without additional schema changes."""


def downgrade() -> None:
    """Unmerge divergent migration heads without additional schema changes."""

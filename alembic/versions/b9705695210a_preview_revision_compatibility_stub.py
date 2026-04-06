"""Compatibility stub for legacy preview-app revision.

Revision ID: b9705695210a
Revises: l1m2n3o4p5q6
Create Date: 2026-04-06 11:15:00.000000
"""

revision = "b9705695210a"
down_revision = "l1m2n3o4p5q6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve an orphaned revision id that still exists in the dev review DB."""


def downgrade() -> None:
    """Downgrade is a no-op for the compatibility stub."""

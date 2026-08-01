"""Merge the desk-latency and metric-version migration heads.

Revision ID: 715a9c0d1e2f
Revises: a456d26aef9a, d6e7f8a9b0c1
Create Date: 2026-07-30
"""

from typing import Sequence, Union

revision: str = "715a9c0d1e2f"
down_revision: Union[str, tuple[str, str], None] = (
    "a456d26aef9a",
    "d6e7f8a9b0c1",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Join the two existing migration branches without changing schema."""


def downgrade() -> None:
    """Restore the two independent heads when rolling back the merge."""

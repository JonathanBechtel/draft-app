"""Merge player taxonomy and news relevance migration heads.

Revision ID: n3o4p5q6r7s8
Revises: e6f7g8h9i0j1, m2n3o4p5q6r7
Create Date: 2026-05-12
"""

from typing import Sequence, Union


revision: str = "n3o4p5q6r7s8"
down_revision: Union[str, tuple[str, str], None] = (
    "e6f7g8h9i0j1",
    "m2n3o4p5q6r7",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge divergent migration heads without additional schema changes."""


def downgrade() -> None:
    """Unmerge divergent migration heads without additional schema changes."""

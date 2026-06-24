"""Merge the two divergent Summer League migration heads.

Revision ID: c0ffee5a1b2c
Revises: bb20c6f83560, e5f6a7b8c9d0
Create Date: 2026-06-23

``main`` carried two parallel alembic heads from independent Summer League
migrations (``bb20c6f83560`` round-label index and ``e5f6a7b8c9d0`` WS/82 &
VORP/82 columns), which makes ``alembic upgrade head`` ambiguous. This is a
no-op merge that reunites them into a single head so subsequent migrations
(``d8a7f6c5b4e3`` draft_results) have one unambiguous parent.
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "c0ffee5a1b2c"
down_revision: Union[str, Sequence[str], None] = ("bb20c6f83560", "e5f6a7b8c9d0")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: this revision only reunites two heads."""


def downgrade() -> None:
    """No-op: splitting back into two heads needs no DDL."""

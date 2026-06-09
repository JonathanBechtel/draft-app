"""Merge stub management and Summer League migration heads.

Revision ID: c0ffee123456
Revises: 0ade42c64694, b6c7d8e9f0a1
Create Date: 2026-06-09

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "c0ffee123456"
down_revision: Union[str, tuple[str, str], None] = (
    "0ade42c64694",
    "b6c7d8e9f0a1",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge migration heads without changing schema."""


def downgrade() -> None:
    """Split migration history back into the prior heads."""

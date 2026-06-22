"""merge all heads

Revision ID: 1a48da7e881c
Revises: 0ade42c64694, 1233bc724a9f, b6c7d8e9f0a1, c7d8e9f0a1b2, e5f6a7b8c9d0, l1m2n3o4p5q6, m2n3o4p5q6r7, x3y4z5a6b7c8, y4z5a6b7c8d9
Create Date: 2026-06-22

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "1a48da7e881c"
down_revision: Union[str, tuple[str, ...], None] = (
    "0ade42c64694",
    "1233bc724a9f",
    "b6c7d8e9f0a1",
    "c7d8e9f0a1b2",
    "e5f6a7b8c9d0",
    "l1m2n3o4p5q6",
    "m2n3o4p5q6r7",
    "x3y4z5a6b7c8",
    "y4z5a6b7c8d9",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

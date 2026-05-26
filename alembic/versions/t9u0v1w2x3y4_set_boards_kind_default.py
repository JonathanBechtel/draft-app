"""Apply the boards.kind server_default that the unify_boards migration intended.

Revision ID: t9u0v1w2x3y4
Revises: s8t9u0v1w2x3
Create Date: 2026-05-26

The unify_boards revision (s8t9u0v1w2x3) added ``boards.kind`` with
``server_default="BIG_BOARD"`` in the model and in the migration. On at
least one environment (dev) the column landed without the default —
likely because the table was bootstrapped via
``SQLModel.metadata.create_all`` and then the alembic head was stamped
without re-running the column addition. The result is that
``board_service.create_board`` — which omits ``kind`` from the INSERT
and relies on the server-side default — fails on dev with
``NotNullViolationError`` even though the schema declares the default.

This revision re-applies ``DEFAULT 'BIG_BOARD'`` to the column so the
live DB matches what the model declares. It's idempotent: re-applying
the same default on a DB that already has it is a no-op.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision: str = "t9u0v1w2x3y4"
down_revision: Union[str, None] = "s8t9u0v1w2x3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KIND_ENUM = postgresql.ENUM(
    "BIG_BOARD", "MOCK_DRAFT", name="board_kind_enum", create_type=False
)


def upgrade() -> None:
    op.alter_column(
        "boards",
        "kind",
        server_default="BIG_BOARD",
        existing_type=_KIND_ENUM,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "boards",
        "kind",
        server_default=None,
        existing_type=_KIND_ENUM,
        existing_nullable=False,
    )

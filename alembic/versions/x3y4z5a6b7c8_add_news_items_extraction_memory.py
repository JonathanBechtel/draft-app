"""Add board-extraction memory columns to news_items.

Adds two columns used by the board auto-ingest worker to avoid retrying
permanent failures and to throttle transient ones:

- ``last_extraction_attempted_at``: timestamp of the most recent extraction
  attempt against the article.
- ``last_extraction_result``: outcome of that attempt, stored as the
  ``board_extraction_result_enum`` Postgres enum.

Mirrors ``app/schemas/news_items.py`` (``BoardExtractionResult`` +
``NewsItem.last_extraction_attempted_at`` / ``.last_extraction_result``).

Revision ID: x3y4z5a6b7c8
Revises: w2x3y4z5a6b7
Create Date: 2026-05-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "x3y4z5a6b7c8"
down_revision: Union[str, None] = "w2x3y4z5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RESULT_ENUM_NAME = "board_extraction_result_enum"
RESULT_VALUES = (
    "SUCCESS",
    "PAYWALLED",
    "NO_ENTRIES",
    "UNRESOLVABLE",
    "TRANSIENT_ERROR",
)


def upgrade() -> None:
    # Create the enum type explicitly (create_type=False so the column add
    # below does not try to re-create it). checkfirst keeps the migration
    # idempotent against partially-applied states.
    result_enum = postgresql.ENUM(
        *RESULT_VALUES, name=RESULT_ENUM_NAME, create_type=False
    )
    result_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "news_items",
        sa.Column(
            "last_extraction_attempted_at",
            sa.DateTime(),
            nullable=True,
        ),
    )
    op.add_column(
        "news_items",
        sa.Column(
            "last_extraction_result",
            result_enum,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("news_items", "last_extraction_result")
    op.drop_column("news_items", "last_extraction_attempted_at")

    bind = op.get_bind()
    sa.Enum(name=RESULT_ENUM_NAME).drop(bind, checkfirst=True)

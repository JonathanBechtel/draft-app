"""Add is_sticky flag to news_items.

Allows pinning a single news item to the top of the homepage and /news
feeds. The single-sticky invariant is enforced at the service layer
(setting is_sticky=True on one row unsets it on all others).

Revision ID: o4p5q6r7s8t9
Revises: n3o4p5q6r7s8
Create Date: 2026-05-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision: str = "o4p5q6r7s8t9"
down_revision: Union[str, None] = "n3o4p5q6r7s8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "news_items",
        sa.Column(
            "is_sticky",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Unique partial index enforces the "at most one sticky row" invariant
    # at the DB level, so a concurrent admin write that tries to pin a
    # second article hits a constraint violation rather than silently
    # leaving two rows with is_sticky=true. The partial WHERE also keeps
    # the index O(1) for the "fetch the sticky item" lookup and adds no
    # cost to inserts of non-sticky rows (which is ~all of them).
    op.create_index(
        "ix_news_items_is_sticky",
        "news_items",
        ["is_sticky"],
        unique=True,
        postgresql_where=sa.text("is_sticky = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_news_items_is_sticky", table_name="news_items")
    op.drop_column("news_items", "is_sticky")

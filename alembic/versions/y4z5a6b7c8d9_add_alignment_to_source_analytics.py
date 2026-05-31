"""Add alignment (Spearman) column to source_analytics.

Adds a nullable ``alignment`` float storing the Spearman rank correlation
(−1..1) between a source's board ranks and the consensus ranks over the
players they share. Surfaced to users as a 0–100 "alignment score" on the
consensus page. NULL when a source ranked fewer than 3 consensus players.

Mirrors ``app/schemas/consensus.py`` (``SourceAnalytics.alignment``).

Revision ID: y4z5a6b7c8d9
Revises: x3y4z5a6b7c8
Create Date: 2026-05-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "y4z5a6b7c8d9"
down_revision: Union[str, None] = "x3y4z5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_analytics",
        sa.Column("alignment", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source_analytics", "alignment")

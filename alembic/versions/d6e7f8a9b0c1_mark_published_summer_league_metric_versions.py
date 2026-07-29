"""Track whether a Summer League metric projection was published."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "summer_league_metric_contexts",
    "summer_league_player_seasons",
)


def upgrade() -> None:
    """Add publication markers and backfill the versions already exposed."""
    for table in _TABLES:
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "published_at" not in columns:
            op.add_column(
                table, sa.Column("published_at", sa.DateTime(), nullable=True)
            )
        op.execute(
            sa.text(
                f"UPDATE {table} SET published_at = COALESCE(published_at, created_at) "
                "WHERE is_current = true"
            )
        )


def downgrade() -> None:
    """Remove publication markers from the projection tables."""
    for table in reversed(_TABLES):
        inspector = sa.inspect(op.get_bind())
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "published_at" in columns:
            op.drop_column(table, "published_at")

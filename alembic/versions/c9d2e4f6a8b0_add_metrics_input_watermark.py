"""Add the durable Summer League metrics input watermark."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "c9d2e4f6a8b0"
down_revision: Union[str, None] = "b8e1c92a77d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists() -> bool:
    """Handle fresh installs whose metadata-based table migration sees this model."""
    columns = sa.inspect(op.get_bind()).get_columns("summer_league_pipeline_states")
    return any(column["name"] == "last_metrics_input_watermark" for column in columns)


def upgrade() -> None:
    """Store the last content watermark used for a successful full rebuild."""
    if not _column_exists():
        op.add_column(
            "summer_league_pipeline_states",
            sa.Column("last_metrics_input_watermark", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    """Remove the metrics input watermark."""
    if _column_exists():
        op.drop_column(
            "summer_league_pipeline_states",
            "last_metrics_input_watermark",
        )

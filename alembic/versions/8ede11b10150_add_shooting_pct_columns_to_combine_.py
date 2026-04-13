"""add shooting pct columns to combine_shooting_results.

Revision ID: 8ede11b10150
Revises: 327ae506058d
Create Date: 2026-03-23 08:03:27.273563
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
revision = "8ede11b10150"
down_revision = "327ae506058d"
branch_labels = None
depends_on = None

PCT_COLUMNS = [
    "off_dribble_pct",
    "spot_up_pct",
    "three_point_star_pct",
    "midrange_star_pct",
    "three_point_side_pct",
    "midrange_side_pct",
    "free_throw_pct",
]

# Maps pct column → (fgm_column, fga_column) for backfill
DRILL_PAIRS = {
    "off_dribble_pct": ("off_dribble_fgm", "off_dribble_fga"),
    "spot_up_pct": ("spot_up_fgm", "spot_up_fga"),
    "three_point_star_pct": ("three_point_star_fgm", "three_point_star_fga"),
    "midrange_star_pct": ("midrange_star_fgm", "midrange_star_fga"),
    "three_point_side_pct": ("three_point_side_fgm", "three_point_side_fga"),
    "midrange_side_pct": ("midrange_side_fgm", "midrange_side_fga"),
    "free_throw_pct": ("free_throw_fgm", "free_throw_fga"),
}


def upgrade() -> None:
    for col in PCT_COLUMNS:
        op.add_column(
            "combine_shooting_results",
            sa.Column(col, sa.Float(), nullable=True),
        )

    # Backfill existing rows
    for pct_col, (fgm_col, fga_col) in DRILL_PAIRS.items():
        op.execute(
            f"UPDATE combine_shooting_results "
            f"SET {pct_col} = ROUND(({fgm_col}::numeric / {fga_col}) * 100, 1) "
            f"WHERE {fga_col} IS NOT NULL AND {fga_col} > 0 "
            f"AND {fgm_col} IS NOT NULL"
        )


def downgrade() -> None:
    for col in reversed(PCT_COLUMNS):
        op.drop_column("combine_shooting_results", col)

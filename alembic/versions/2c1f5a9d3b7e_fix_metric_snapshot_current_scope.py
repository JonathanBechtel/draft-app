"""fix metric snapshot current scope + cohort versioning

Revision ID: 2c1f5a9d3b7e
Revises: 8c9a3d9c1f4e
Create Date: 2025-12-13
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa


revision = "2c1f5a9d3b7e"
down_revision = "8c9a3d9c1f4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "uq_metric_snapshots_current",
        table_name="metric_snapshots",
        postgresql_where=sa.text("is_current = true"),
    )
    op.drop_constraint(
        "uq_metric_snapshots_src_run_ver",
        "metric_snapshots",
        type_="unique",
    )

    op.create_unique_constraint(
        "uq_metric_snapshots_src_run_ver",
        "metric_snapshots",
        ["cohort", "source", "run_key", "version"],
    )
    op.create_index(
        "uq_metric_snapshots_current",
        "metric_snapshots",
        [
            "cohort",
            "source",
            sa.text("coalesce(season_id, -1)"),
            sa.text("coalesce(position_scope_parent, '__none__')"),
            sa.text("coalesce(position_scope_fine, '__none__')"),
        ],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_metric_snapshots_current",
        table_name="metric_snapshots",
        postgresql_where=sa.text("is_current = true"),
    )
    op.drop_constraint(
        "uq_metric_snapshots_src_run_ver",
        "metric_snapshots",
        type_="unique",
    )

    op.create_unique_constraint(
        "uq_metric_snapshots_src_run_ver",
        "metric_snapshots",
        ["source", "run_key", "version"],
    )
    op.create_index(
        "uq_metric_snapshots_current",
        "metric_snapshots",
        ["source", "run_key"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
    )

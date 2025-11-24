"""feat: expand position taxonomy for combine + metrics snapshots

Revision ID: 7f2c9f6d0e2a
Revises: fdb8c05957b9
Create Date: 2025-05-29 04:35:00.000000
"""

from __future__ import annotations

from typing import Any, Dict

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.models.position_taxonomy import derive_position_tags

# revision identifiers, used by Alembic.
revision = "7f2c9f6d0e2a"
down_revision = "fdb8c05957b9"
branch_labels = None
depends_on = None


COMBINE_TABLES = (
    "combine_anthro",
    "combine_agility",
    "combine_shooting_results",
)


def _backfill_positions(table_name: str) -> None:
    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column("id", sa.Integer()),
        sa.column("raw_position", sa.String()),
        sa.column("position_fine", sa.String()),
        sa.column("position_parents", postgresql.JSONB()),
    )
    rows = bind.execute(sa.select(table.c.id, table.c.raw_position)).fetchall()
    for row in rows:
        fine, parents = derive_position_tags(row.raw_position)
        updates: Dict[str, Any] = {}
        if fine:
            updates["position_fine"] = fine
        if parents:
            updates["position_parents"] = parents
        if updates:
            bind.execute(table.update().where(table.c.id == row.id).values(**updates))


def upgrade() -> None:
    for table_name in COMBINE_TABLES:
        op.alter_column(table_name, "pos", new_column_name="raw_position")
        op.add_column(
            table_name,
            sa.Column("position_fine", sa.String(), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("position_parents", postgresql.JSONB(), nullable=True),
        )
        _backfill_positions(table_name)

    op.add_column(
        "metric_snapshots",
        sa.Column("position_scope_fine", sa.String(), nullable=True),
    )
    op.add_column(
        "metric_snapshots",
        sa.Column("position_scope_parent", sa.String(), nullable=True),
    )

    snapshots = sa.table(
        "metric_snapshots",
        sa.column("id", sa.Integer()),
        sa.column("position_scope", sa.String()),
        sa.column("position_scope_parent", sa.String()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(snapshots.c.id, snapshots.c.position_scope).where(
            snapshots.c.position_scope.isnot(None)
        )
    ).fetchall()
    for row in rows:
        legacy = row.position_scope
        if legacy == "center":
            parent = "big"
        else:
            parent = legacy
        bind.execute(
            snapshots.update()
            .where(snapshots.c.id == row.id)
            .values(position_scope_parent=parent)
        )

    op.drop_column("metric_snapshots", "position_scope")
    op.execute("DROP TYPE IF EXISTS metric_position_enum")


def downgrade() -> None:
    metric_position_enum = postgresql.ENUM(
        "guard", "forward", "center", name="metric_position_enum"
    )
    metric_position_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "metric_snapshots",
        sa.Column(
            "position_scope",
            metric_position_enum,
            nullable=True,
        ),
    )
    snapshots = sa.table(
        "metric_snapshots",
        sa.column("id", sa.Integer()),
        sa.column("position_scope", metric_position_enum),
        sa.column("position_scope_parent", sa.String()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(snapshots.c.id, snapshots.c.position_scope_parent)
    ).fetchall()
    for row in rows:
        parent = row.position_scope_parent
        legacy = None
        if parent == "guard":
            legacy = "guard"
        elif parent == "forward":
            legacy = "forward"
        elif parent == "big":
            legacy = "center"
        if legacy:
            bind.execute(
                snapshots.update()
                .where(snapshots.c.id == row.id)
                .values(position_scope=legacy)
            )

    op.drop_column("metric_snapshots", "position_scope_parent")
    op.drop_column("metric_snapshots", "position_scope_fine")

    for table_name in COMBINE_TABLES:
        op.drop_column(table_name, "position_parents")
        op.drop_column(table_name, "position_fine")
        op.alter_column(table_name, "raw_position", new_column_name="pos")

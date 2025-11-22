"""refactor positions

Revision ID: a3335e6095f7
Revises: 0cdc5f018a72
Create Date: 2025-11-22 00:45:00.000000

"""

from typing import List, Optional
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import json

# revision identifiers, used by Alembic.
revision = "a3335e6095f7"
down_revision = "0cdc5f018a72"
branch_labels = None
depends_on = None

# --- Taxonomy Logic Copied from app/models/position_taxonomy.py ---
_BASE_PARENT_MAP = {
    "PG": {"guard"},
    "SG": {"guard"},
    "G": {"guard"},
    "SF": {"wing", "forward"},
    "PF": {"forward", "big"},
    "F": {"forward"},
    "C": {"big"},
}


def get_parents_for_fine(fine: Optional[str]) -> List[str]:
    if not fine:
        return []
    tokens = fine.split("_")
    parents: List[str] = []
    seen = set()
    for token in tokens:
        upper = token.upper()
        parent_set = _BASE_PARENT_MAP.get(upper, set())
        for parent in parent_set:
            if parent not in seen:
                parents.append(parent)
                seen.add(parent)
    return parents


# --- End Taxonomy Logic ---


def upgrade() -> None:
    # 1. Add parents column to positions
    op.add_column(
        "positions",
        sa.Column("parents", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # 2. Populate parents
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, code FROM positions")).fetchall()
    for row in rows:
        pid, code = row
        parents = get_parents_for_fine(code)
        if parents:
            bind.execute(
                sa.text("UPDATE positions SET parents = :parents WHERE id = :id"),
                {"parents": json.dumps(parents), "id": pid},
            )

    # 3. Rename player_status.position -> raw_position
    op.alter_column("player_status", "position", new_column_name="raw_position")

    # 4. Drop redundant columns from combine tables
    combine_tables = ["combine_anthro", "combine_agility", "combine_shooting_results"]
    for table in combine_tables:
        op.drop_column(table, "position_fine")
        op.drop_column(table, "position_parents")


def downgrade() -> None:
    # 1. Add back columns to combine tables
    combine_tables = ["combine_anthro", "combine_agility", "combine_shooting_results"]
    for table in combine_tables:
        op.add_column(
            table,
            sa.Column(
                "position_parents",
                postgresql.JSONB(astext_type=sa.Text()),
                autoincrement=False,
                nullable=True,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "position_fine", sa.VARCHAR(), autoincrement=False, nullable=True
            ),
        )
        op.create_index(
            f"ix_{table}_position_fine", table, ["position_fine"], unique=False
        )

    # 2. Rename raw_position -> position
    op.alter_column("player_status", "raw_position", new_column_name="position")

    # 3. Drop parents from positions
    op.drop_column("positions", "parents")

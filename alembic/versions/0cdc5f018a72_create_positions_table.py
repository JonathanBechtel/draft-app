"""create positions table

Revision ID: 0cdc5f018a72
Revises: fdb8c05957b9
Create Date: 2025-11-22 00:35:00.000000

"""

from typing import List, Optional, Dict
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision = "0cdc5f018a72"
down_revision = "b1c2d3e4f5a7"
branch_labels = None
depends_on = None

# --- Taxonomy Logic Copied from app/models/position_taxonomy.py ---
_BASE_ORDER = ["PG", "SG", "SF", "PF", "C", "G", "F"]
_BASE_NORMALIZATION = {
    "PG": "PG",
    "POINT": "PG",
    "POINTGUARD": "PG",
    "SG": "SG",
    "SHOOTING": "SG",
    "SHOOTINGGUARD": "SG",
    "SF": "SF",
    "SMALL": "SF",
    "SMALLFORWARD": "SF",
    "PF": "PF",
    "POWER": "PF",
    "POWERFORWARD": "PF",
    "C": "C",
    "CENTER": "C",
    "G": "G",
    "F": "F",
}


def _tokenize_raw_position(raw: str) -> List[str]:
    cleaned = raw.replace("/", "-").replace(" ", "").upper()
    tokens = [tok for tok in cleaned.split("-") if tok]
    normalized: List[str] = []
    for token in tokens:
        canonical = _BASE_NORMALIZATION.get(token)
        if canonical is None:
            continue
        normalized.append(canonical)
    if not normalized:
        return []
    unique: List[str] = []
    for token in normalized:
        if token not in unique:
            unique.append(token)
    order_index = {code: idx for idx, code in enumerate(_BASE_ORDER)}
    unique.sort(key=lambda code: order_index.get(code, len(_BASE_ORDER)))
    return unique


def derive_position_tags(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    tokens = _tokenize_raw_position(raw)
    if not tokens:
        return None
    fine_token = "_".join(token.lower() for token in tokens)
    return fine_token


# --- End Taxonomy Logic ---


def upgrade() -> None:
    # 1. Create positions table
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_positions_code"), "positions", ["code"], unique=True)

    # 2. Add position_id columns
    tables = [
        "player_status",
        "combine_anthro",
        "combine_agility",
        "combine_shooting_results",
    ]
    for table in tables:
        op.add_column(table, sa.Column("position_id", sa.Integer(), nullable=True))
        op.create_index(
            op.f(f"ix_{table}_position_id"), table, ["position_id"], unique=False
        )
        op.create_foreign_key(
            f"fk_{table}_position_id", table, "positions", ["position_id"], ["id"]
        )

    # 3. Data Migration
    bind = op.get_bind()

    # Seed standard positions
    # We'll seed based on what we encounter + standard ones
    # But let's start with the preset ones to ensure they have IDs
    FINE_SCOPE_PRESET = [
        "pg",
        "sg",
        "sf",
        "pf",
        "c",
        "pg-sg",
        "sg-sf",
        "sf-pf",
        "pf-c",
        "g",
        "f",  # Adding these as they appear in base order
    ]
    # Also combinations from base order if needed, but let's just dynamic insert

    # Helper to get or create position ID
    position_cache: Dict[str, int] = {}  # code -> id

    def get_or_create_position(code: str) -> int:
        if code in position_cache:
            return position_cache[code]

        # Check db
        res = bind.execute(
            sa.text("SELECT id FROM positions WHERE code = :code"), {"code": code}
        ).scalar()
        if res:
            position_cache[code] = res
            return res

        # Insert
        res = bind.execute(
            sa.text("INSERT INTO positions (code) VALUES (:code) RETURNING id"),
            {"code": code},
        ).scalar()
        position_cache[code] = res
        return res

    # Pre-seed common ones
    for code in FINE_SCOPE_PRESET:
        get_or_create_position(code)

    # Backfill player_status
    # Optimization: Fetch distinct positions first, then update in bulk
    # source column: position (string)
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT position FROM player_status WHERE position IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        raw_pos = row[0]
        if not raw_pos:
            continue
        fine = derive_position_tags(raw_pos)
        if fine:
            pos_id = get_or_create_position(fine)
            bind.execute(
                sa.text(
                    "UPDATE player_status SET position_id = :pos_id WHERE position = :raw_pos"
                ),
                {"pos_id": pos_id, "raw_pos": raw_pos},
            )

    # Backfill combine tables
    # Optimization: Fetch distinct combinations of (position_fine, raw_position)
    combine_tables = ["combine_anthro", "combine_agility", "combine_shooting_results"]
    for table in combine_tables:
        # We group by both columns to cover all unique cases
        rows = bind.execute(
            sa.text(f"SELECT DISTINCT position_fine, raw_position FROM {table}")
        ).fetchall()
        for row in rows:
            fine, raw = row
            target_code = None
            if fine:
                target_code = fine
            elif raw:
                target_code = derive_position_tags(raw)

            if target_code:
                pos_id = get_or_create_position(target_code)
                # Update all rows matching this specific combination
                # Handle NULLs in WHERE clause carefully
                if fine is None and raw is None:
                    continue  # Should not happen based on logic above but good safety

                clauses = []
                params = {"pos_id": pos_id}

                if fine is not None:
                    clauses.append("position_fine = :fine")
                    params["fine"] = fine
                else:
                    clauses.append("position_fine IS NULL")

                if raw is not None:
                    clauses.append("raw_position = :raw")
                    params["raw"] = raw
                else:
                    clauses.append("raw_position IS NULL")

                where_clause = " AND ".join(clauses)
                bind.execute(
                    sa.text(
                        f"UPDATE {table} SET position_id = :pos_id WHERE {where_clause}"
                    ),
                    params,
                )


def downgrade() -> None:
    tables = [
        "player_status",
        "combine_anthro",
        "combine_agility",
        "combine_shooting_results",
    ]
    for table in tables:
        op.drop_constraint(f"fk_{table}_position_id", table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_position_id"), table_name=table)
        op.drop_column(table, "position_id")

    op.drop_index(op.f("ix_positions_code"), table_name="positions")
    op.drop_table("positions")

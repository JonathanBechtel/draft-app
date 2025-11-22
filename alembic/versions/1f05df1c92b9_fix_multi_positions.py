"""fix multi positions

Revision ID: 1f05df1c92b9
Revises: a3335e6095f7
Create Date: 2025-11-22 01:00:00.000000

"""

from typing import List, Optional, Dict
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
import json

# revision identifiers, used by Alembic.
revision = "1f05df1c92b9"
down_revision = "a3335e6095f7"
branch_labels = None
depends_on = None

# --- Taxonomy Logic Copied/Updated from app/models/position_taxonomy.py ---
_BASE_PARENT_MAP = {
    "PG": {"guard"},
    "SG": {"guard"},
    "G": {"guard"},
    "SF": {"wing", "forward"},
    "PF": {"forward", "big"},
    "F": {"forward"},
    "C": {"big"},
}
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
    "GUARD": "G",
    "F": "F",
    "FORWARD": "F",
}
_BASE_ORDER = ["PG", "SG", "SF", "PF", "C", "G", "F"]


def _tokenize_raw_position(raw: str) -> List[str]:
    # Normalize delimiters: " and " -> "-", "/" -> "-"
    s = raw.replace(" and ", "-").replace("/", "-")
    # Remove remaining spaces and uppercase
    cleaned = s.replace(" ", "").upper()
    tokens = [tok for tok in cleaned.split("-") if tok]
    normalized: List[str] = []
    for token in tokens:
        norm = _BASE_NORMALIZATION.get(token)
        if norm:
            normalized.append(norm)
    return normalized


def derive_position_tags(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    tokens = _tokenize_raw_position(raw)
    if not tokens:
        return None

    # Sort based on base order
    order_index = {code: i for i, code in enumerate(_BASE_ORDER)}
    unique = sorted(list(set(tokens)), key=lambda code: order_index.get(code, 99))
    fine_token = "_".join(token.lower() for token in unique)
    return fine_token


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
    return sorted(parents)


# --- End Taxonomy Logic ---


def upgrade() -> None:
    bind = op.get_bind()

    # Helper to get or create position ID
    position_cache: Dict[str, int] = {}

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

        # Insert with parents
        parents = get_parents_for_fine(code)
        res = bind.execute(
            sa.text(
                "INSERT INTO positions (code, parents) VALUES (:code, :parents) RETURNING id"
            ),
            {"code": code, "parents": json.dumps(parents)},
        ).scalar()
        position_cache[code] = res
        return res

    # Fetch distinct raw_positions from player_status where position_id is NULL but raw_position is NOT NULL
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT raw_position FROM player_status WHERE position_id IS NULL AND raw_position IS NOT NULL"
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
                    "UPDATE player_status SET position_id = :pos_id WHERE raw_position = :raw_pos AND position_id IS NULL"
                ),
                {"pos_id": pos_id, "raw_pos": raw_pos},
            )


def downgrade() -> None:
    pass

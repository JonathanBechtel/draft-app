"""Add Summer League Desk projection tables (T1-T4).

Revision ID: b2f10d7f542d
Revises: 2f09df4af11c
Create Date: 2026-07-09 11:22:46.729270

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
    SummerLeagueDeskStoryline,
)

# revision identifiers, used by Alembic.
revision: str = "b2f10d7f542d"
down_revision: Union[str, None] = "2f09df4af11c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUMMER_LEAGUE_DESK_TABLES = [
    SummerLeagueCohortBaseline.__table__,  # type: ignore[attr-defined]
    SummerLeagueDeskPlayerGrade.__table__,  # type: ignore[attr-defined]
    SummerLeagueDeskStoryline.__table__,  # type: ignore[attr-defined]
    SummerLeagueDeskSlate.__table__,  # type: ignore[attr-defined]
]

# The four PG enum types this migration introduces (behavior spec §10). Dropped
# explicitly in downgrade() -- see the note there for why.
SUMMER_LEAGUE_DESK_ENUM_NAMES = [
    "summer_league_desk_cohort_kind_enum",
    "summer_league_desk_grain_enum",
    "summer_league_desk_grade_enum",
    "summer_league_desk_trigger_type_enum",
]


def upgrade() -> None:
    """Create the Summer League Desk projection tables (T1-T4)."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=SUMMER_LEAGUE_DESK_TABLES,
    )


def downgrade() -> None:
    """Drop the Summer League Desk projection tables (T1-T4) and their enum types.

    Deliberately does NOT use ``SQLModel.metadata.drop_all(bind=..., tables=[...])``
    here: that call scans the *entire* app metadata for PG enum types to drop --
    not just the ones used by the given ``tables`` -- because alembic/env.py
    imports every ``app.schemas`` module, so ``SQLModel.metadata`` always holds
    the full, app-wide enum set. Confirmed via ``alembic downgrade --sql`` against
    a prod-like schema: it emitted ``DROP TYPE`` for ~38 unrelated enums (e.g.
    ``board_status_enum``) and aborted once it hit one still referenced by a live
    table. ``op.drop_table`` + explicit, scoped ``postgresql.ENUM(...).drop()``
    calls (mirroring the prior migration, ``2f09df4af11c``) avoid that entirely.
    """
    bind = op.get_bind()

    for table in reversed(SUMMER_LEAGUE_DESK_TABLES):
        op.drop_table(table.name)  # type: ignore[attr-defined]

    for enum_name in SUMMER_LEAGUE_DESK_ENUM_NAMES:
        postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)

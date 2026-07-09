"""Add Event Desk registry + state tables (events, event_desk_state).

Revision ID: 77f1f90be54a
Revises: e7c75f3063ec
Create Date: 2026-07-09 12:02:43.083069
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

from app.schemas.event_desk import Event, EventDeskState

# revision identifiers, used by Alembic.
revision: str = "77f1f90be54a"
down_revision: Union[str, None] = "e7c75f3063ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EVENT_DESK_TABLES = [
    Event.__table__,  # type: ignore[attr-defined]
    EventDeskState.__table__,  # type: ignore[attr-defined]
]

# The four PG enum types this migration introduces (event-desk-framework.md
# "Data model delta"). Dropped explicitly in downgrade() -- see the note there.
EVENT_DESK_ENUM_NAMES = [
    "event_type_enum",
    "event_calendar_source_enum",
    "event_lifecycle_phase_enum",
    "event_daily_state_enum",
]


def upgrade() -> None:
    """Create the generic Event Desk registry + state tables."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=EVENT_DESK_TABLES,
    )


def downgrade() -> None:
    """Drop the Event Desk registry + state tables and their enum types.

    Deliberately does NOT use ``SQLModel.metadata.drop_all(bind=..., tables=[...])``
    here: that call scans the *entire* app metadata for PG enum types to drop --
    not just the ones used by the given ``tables`` -- because alembic/env.py
    imports every ``app.schemas`` module, so ``SQLModel.metadata`` always holds
    the full, app-wide enum set. Confirmed via ``alembic downgrade --sql`` against
    this revision: a bare ``drop_all`` would emit ``DROP TYPE`` for every enum in
    the app (e.g. ``board_status_enum``, ``summer_league_desk_grade_enum``) and
    abort on the first one still referenced by a live table. ``op.drop_table`` +
    explicit, scoped ``postgresql.ENUM(...).drop()`` calls (mirroring the prior
    precedent, ``b2f10d7f542d`` / ``2f09df4af11c``) avoid that entirely.
    """
    bind = op.get_bind()

    for table in reversed(EVENT_DESK_TABLES):
        op.drop_table(table.name)  # type: ignore[attr-defined]

    for enum_name in EVENT_DESK_ENUM_NAMES:
        postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)

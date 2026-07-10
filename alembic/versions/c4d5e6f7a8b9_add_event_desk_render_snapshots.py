"""Add Event Desk render snapshot table (event_desk_render_snapshots).

Revision ID: c4d5e6f7a8b9
Revises: a2b3c4d5e6f7
Create Date: 2026-07-10 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.event_desk_render_snapshot import EventDeskRenderSnapshot

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EVENT_DESK_RENDER_SNAPSHOT_TABLES = [
    EventDeskRenderSnapshot.__table__,  # type: ignore[attr-defined]
]

# This migration introduces NO new enum type: `daily_state` reuses the existing
# `event_daily_state_enum` Postgres type created by `77f1f90be54a`
# (`app.schemas.event_desk.EventDeskState`). Downgrade must therefore NOT drop that
# enum -- it is still live under `event_desk_state`. This is the guardrail scenario
# in reverse: the risk here isn't a bare `drop_all` sweeping unrelated enums (there
# is nothing enum-specific to this migration to sweep), it's a copy-paste of the
# `b2f10d7f542d` / `77f1f90be54a` enum-drop block accidentally dropping a *shared*
# enum still referenced by `event_desk_state`. So downgrade only drops the table.


def upgrade() -> None:
    """Create the Event Desk render snapshot table."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=EVENT_DESK_RENDER_SNAPSHOT_TABLES,
    )


def downgrade() -> None:
    """Drop the Event Desk render snapshot table.

    Deliberately does NOT use ``SQLModel.metadata.drop_all(bind=..., tables=[...])``
    here: that call scans the *entire* app metadata for PG enum types to drop -- not
    just the ones this migration introduced -- because alembic/env.py imports every
    ``app.schemas`` module, so ``SQLModel.metadata`` always holds the full, app-wide
    enum set. Confirmed via the same precedent as ``b2f10d7f542d`` / ``77f1f90be54a``:
    a bare ``drop_all`` would emit ``DROP TYPE`` for every enum in the app (including
    ``event_daily_state_enum``, which this table's own ``daily_state`` column uses but
    does NOT own -- ``event_desk_state`` still references it) and abort on the first
    one still referenced by a live table. A scoped ``op.drop_table`` avoids that
    entirely; there is no scoped enum drop here because this migration owns no enum
    type of its own (``daily_state`` reuses ``event_daily_state_enum`` rather than
    minting a duplicate).
    """
    for table in reversed(EVENT_DESK_RENDER_SNAPSHOT_TABLES):
        op.drop_table(table.name)  # type: ignore[attr-defined]

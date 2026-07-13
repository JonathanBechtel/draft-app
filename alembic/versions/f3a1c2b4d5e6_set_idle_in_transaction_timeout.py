"""Set a role-level idle_in_transaction_session_timeout guard.

Revision ID: f3a1c2b4d5e6
Revises: b064dbc9f8bf
Create Date: 2026-07-13 00:00:00.000000

Permanent fix for #572: a Summer League ingestion/roster session was left
``idle in transaction`` for 24+ minutes after a ``SELECT`` against
``summer_league_source_players``. It was routed through Neon's pgbouncer
pooler (``application_name=pgbouncer``), so when the originating cron process
died/stalled mid-transaction the pooler kept its server-side connection open
and idle-in-transaction, still holding the ``players_master`` locks the
normalization loop had taken while inserting stub rows. That blocked a
deploy's ``CREATE TABLE ... FK players_master`` for ~20 minutes.

Every application session *is* already scoped in an ``async with`` block, so
this is not a missing-context-manager bug: it is an abandoned transaction with
no server-side reaper. ``idle_in_transaction_session_timeout`` is exactly that
reaper -- Postgres terminates any backend that sits idle *inside a
transaction* longer than the threshold (it never fires while a statement is
actively running, so it cannot kill a legitimately long, actively-working
transaction).

It is set at the *role* level (not via a client connect arg) deliberately:
the connection that leaked was the pooler's own server backend, which connects
to Postgres as this role and inherits the role's ``SET`` defaults. A client
connect arg would only touch the app's client connections, not the pooler's
server connections -- and asyncpg ``server_settings`` for this GUC would be
sent as a startup parameter, which pgbouncer rejects, breaking every pooled
connection. ``ALTER ROLE`` avoids both problems.

180s comfortably exceeds the one legitimate idle-in-transaction window in the
codebase -- the scoreboard schedule refresh fetches an NBA feed inside the
caller's transaction, worst case ~96s (30s timeout x 3 retries + delays) -- so
it never reaps real work, while still reaping a genuine leak ~8x faster than
the 24-minute incident.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "f3a1c2b4d5e6"
down_revision: Union[str, None] = "b064dbc9f8bf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IDLE_IN_TRANSACTION_TIMEOUT = "180s"


def upgrade() -> None:
    """Apply the idle-in-transaction reaper to the connecting role.

    ``CURRENT_USER`` is the role the app (and the pooler's server connections)
    authenticate as, so the default is inherited by every new backend for that
    role, including pooled backends, without needing DB-owner privileges.
    ``idle_in_transaction_session_timeout`` is a ``USERSET`` GUC, so a
    non-superuser role may set its own default.
    """
    op.execute(
        "ALTER ROLE CURRENT_USER "
        f"SET idle_in_transaction_session_timeout = '{IDLE_IN_TRANSACTION_TIMEOUT}'"
    )


def downgrade() -> None:
    """Remove the role-level default, reverting to the server/db default."""
    op.execute("ALTER ROLE CURRENT_USER RESET idle_in_transaction_session_timeout")

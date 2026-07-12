"""Add POSTPONED/CANCELED summer_league_games.status enum values.

Fix #4 (follow-up to #529/#530 + fix #2/#3): a prior fix made postponed games
terminal in the event-agnostic ``GameStatus`` the daily state machine
consumes, but took the no-migration path -- ``summer_league_games.status``
still persisted a postponed game as SCHEDULED (with "PPD" only in
``status_text``). Combined with fix #2 (a failed critical box-score endpoint
now aborts the whole live-ingestion tick), a postponed game inside its tip
window was still selected by ``select_active_window_games`` (it filters on
this column) for a critical box-score refresh whose endpoints will never
return data -- aborting every tick in its window. This migration makes
postponed/canceled a real, persisted status so every consumer that filters on
the status enum excludes it automatically. Never drops/recreates
summer_league_games -- adds two enum values only.

Revision ID: b064dbc9f8bf
Revises: c4d5e6f7a8b9
Create Date: 2026-07-11
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "b064dbc9f8bf"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the POSTPONED and CANCELED game status values.

    Idempotent, matching the precedent set by e7c75f3063ec for this same
    enum: on a from-scratch upgrade, ``create_all`` reflects the updated
    ``SummerLeagueGameStatus`` model and already creates the enum with these
    values, so ``ADD VALUE`` is a no-op; on an existing DB the enum lacks
    them and this adds them. Postgres enum ``ADD VALUE`` cannot run inside a
    transaction block; isolate it in its own autocommit block (matches
    bc9443ccd2b6, l1m2n3o4p5q6, e7c75f3063ec).
    """
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE summer_league_game_status_enum "
            "ADD VALUE IF NOT EXISTS 'postponed'"
        )
        op.execute(
            "ALTER TYPE summer_league_game_status_enum "
            "ADD VALUE IF NOT EXISTS 'canceled'"
        )


def downgrade() -> None:
    """No-op: Postgres cannot drop a single enum value.

    ``ALTER TYPE ... DROP VALUE`` does not exist, and rows may already
    reference POSTPONED/CANCELED by the time this runs. Recreating the type
    without the values would require rewriting every dependent
    column/constraint just to reverse an additive, backwards-compatible
    change. This mirrors the repo's existing no-op precedent for enum-value
    downgrades (bc9443ccd2b6, l1m2n3o4p5q6, e7c75f3063ec): the values are
    inert for any code path that no longer writes them.
    """

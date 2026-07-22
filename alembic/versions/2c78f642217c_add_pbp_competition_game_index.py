"""Add competition-leading index to summer_league_play_by_play_events.

Revision ID: 2c78f642217c
Revises: 9fae26346abb
Create Date: 2026-07-20

Ticket #643 (Competition Context Explorer hardening -- offline rebuild query
perf): the environment rebuild's ``_load_pbp`` step (in
``app/services/summer_league_environment_service.py``) filters play-by-play
events by ``competition_id`` (with the eligible-final-games join and
``GROUP BY competition_id, game_id``), but the only existing PBP index
(``ix_summer_league_pbp_events_game_period_event``) is led by ``game_id``.

`EXPLAIN (ANALYZE, BUFFERS)` against a Neon prod read branch confirmed this
falls back to a full ``Seq Scan`` on ``summer_league_play_by_play_events``:
one competition alone already holds ~80% of the table's ~40k rows, and every
other rebuild-path table (team/player game logs, participation, shot events)
already carries a competition-leading index for the equivalent access
pattern -- this table was the one gap.

This is an additive index on an existing table, so this migration uses a
targeted ``op.create_index``/``op.drop_index`` rather than
``SQLModel.metadata.create_all``/``drop_all`` per the repo migration
convention for existing-table changes.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "2c78f642217c"
down_revision: Union[str, None] = "9fae26346abb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the ``(competition_id, game_id)`` index for PBP rebuild reads.

    ``if_not_exists=True``: the parent table is created by an earlier
    ``SQLModel.metadata.create_all`` migration that reflects the live model
    class, so a from-scratch bootstrap already has this index by the time
    this migration runs (see `16a524075c8e` for the same guard rationale).
    """
    op.create_index(
        "ix_summer_league_pbp_events_competition_game",
        "summer_league_play_by_play_events",
        ["competition_id", "game_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the ``(competition_id, game_id)`` index."""
    op.drop_index(
        "ix_summer_league_pbp_events_competition_game",
        table_name="summer_league_play_by_play_events",
        if_exists=True,
    )

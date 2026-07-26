"""Index every reassignable player FK the merge/safe-delete paths scan.

Revision ID: 3f8c1d47a9b2
Revises: 7d91b31cfe42
Create Date: 2026-07-26

Ticket #681. ``count_inbound_references`` (the safe-delete guard behind stub
deletion) and ``_merge_child_table`` (the merge path) both address every
registered child table by ``<player column> = :player_id``. Seven of those
columns carried no index — a foreign key constraint does not create one — so
each lookup fell back to a Seq Scan of the whole table:

* ``summer_league_play_by_play_events.person1_id`` / ``person2_id`` /
  ``person3_id`` — three separate scans of the ~40k-row (and fastest-growing)
  PBP table per player, newly reachable since #680 derived the guard from the
  classified merge specs
* ``summer_league_desk_storylines.subject_player_id_2``
* ``news_items.player_id``, ``podcast_episodes.player_id``,
  ``source_analytics.biggest_outlier_player_id`` — counted by the guard since
  before #680, same defect

The same gap costs on the write path: with the FK unindexed, Postgres's
RESTRICT check Seq-Scans each child table when a ``players_master`` row is
deleted, and the merge ``UPDATE ... WHERE col = :discard_id`` does too.

All seven columns are nullable and mostly NULL (the second PBP participant, a
duel's second subject, an attributed news item), so each index is partial on
``col IS NOT NULL``. That is free for these lookups: ``col = :player_id`` is
strict, so the planner proves the predicate and still uses the index.

Additive indexes on existing tables, so this uses targeted
``op.create_index``/``op.drop_index`` rather than
``SQLModel.metadata.create_all`` per the repo migration convention.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "3f8c1d47a9b2"
down_revision: Union[str, None] = "7d91b31cfe42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index name, table, column) — one partial index per reassignable player FK.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_summer_league_pbp_events_person1",
        "summer_league_play_by_play_events",
        "person1_id",
    ),
    (
        "ix_summer_league_pbp_events_person2",
        "summer_league_play_by_play_events",
        "person2_id",
    ),
    (
        "ix_summer_league_pbp_events_person3",
        "summer_league_play_by_play_events",
        "person3_id",
    ),
    (
        "ix_summer_league_desk_storylines_subject_player_id_2",
        "summer_league_desk_storylines",
        "subject_player_id_2",
    ),
    ("ix_news_items_player_id", "news_items", "player_id"),
    ("ix_podcast_episodes_player_id", "podcast_episodes", "player_id"),
    (
        "ix_source_analytics_biggest_outlier_player_id",
        "source_analytics",
        "biggest_outlier_player_id",
    ),
)


def upgrade() -> None:
    """Create the seven partial player-leading indexes.

    ``if_not_exists=True``: several of these parent tables are created by an
    earlier ``SQLModel.metadata.create_all`` migration that reflects the live
    model class, so a from-scratch bootstrap already has these indexes by the
    time this migration runs (see ``2c78f642217c`` for the same rationale).
    """
    for name, table, column in _INDEXES:
        op.create_index(
            name,
            table,
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
            if_not_exists=True,
        )


def downgrade() -> None:
    """Drop the seven partial player-leading indexes."""
    for name, table, _column in reversed(_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)

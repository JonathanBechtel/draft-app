"""add competition-leading index on summer_league_shot_events

Supports the pool-baseline aggregation in
``summer_league_shotchart_service._fetch_pool_baseline`` (groups by zone within
a single competition); the existing ``(player_id, competition_id)`` index cannot
serve a competition-only predicate efficiently.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_summer_league_shot_events_competition_zone"
TABLE_NAME = "summer_league_shot_events"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ["competition_id", "shot_zone_basic"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME, if_exists=True)

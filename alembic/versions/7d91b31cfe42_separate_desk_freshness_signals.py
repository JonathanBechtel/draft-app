"""Separate Event Desk content freshness from scheduler observations.

Revision ID: 7d91b31cfe42
Revises: 2c78f642217c
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision: str = "7d91b31cfe42"
down_revision: Union[str, None] = "2c78f642217c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename Desk watermarks in place and add durable pipeline signals."""
    bind = op.get_bind()
    desk_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("event_desk_state")
    }
    if "as_of" in desk_columns:
        op.alter_column(
            "event_desk_state", "as_of", new_column_name="lifecycle_observed_at"
        )
    if "freshness_tick_at" in desk_columns:
        op.alter_column(
            "event_desk_state",
            "freshness_tick_at",
            new_column_name="content_refreshed_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )
    else:
        op.alter_column(
            "event_desk_state",
            "content_refreshed_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )

    pipeline_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("summer_league_pipeline_states")
    }
    additions = (
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_job_image", sa.Text(), nullable=True),
        sa.Column("last_source_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("last_source_advanced_at", sa.DateTime(), nullable=True),
        sa.Column("last_projection_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("last_content_updated", sa.Boolean(), nullable=True),
    )
    for column in additions:
        if column.name not in pipeline_columns:
            op.add_column("summer_league_pipeline_states", column)
    op.execute(
        """
        UPDATE summer_league_pipeline_states
        SET last_completed_at = COALESCE(
            GREATEST(last_succeeded_at, last_failure_at, last_deferred_at),
            updated_at
        )
        WHERE last_completed_at IS NULL
          AND last_outcome IS NOT NULL
        """
    )


def downgrade() -> None:
    """Restore the legacy schema without fabricating content timestamps.

    The legacy non-null column cannot represent a lifecycle-only row. Such
    rows have never produced content, so deleting them is the only truthful
    downgrade; observation or migration time would invent freshness.
    """
    op.drop_column("summer_league_pipeline_states", "last_content_updated")
    op.drop_column("summer_league_pipeline_states", "last_projection_refreshed_at")
    op.drop_column("summer_league_pipeline_states", "last_source_advanced_at")
    op.drop_column("summer_league_pipeline_states", "last_source_refreshed_at")
    op.drop_column("summer_league_pipeline_states", "last_job_image")
    op.drop_column("summer_league_pipeline_states", "last_completed_at")
    op.drop_column("summer_league_pipeline_states", "last_started_at")
    op.execute("DELETE FROM event_desk_state WHERE content_refreshed_at IS NULL")
    op.alter_column(
        "event_desk_state",
        "content_refreshed_at",
        new_column_name="freshness_tick_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )
    op.alter_column(
        "event_desk_state",
        "lifecycle_observed_at",
        new_column_name="as_of",
    )

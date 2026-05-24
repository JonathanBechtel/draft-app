"""Add consensus_snapshots, big_board_consensus, source_analytics tables.

Revision ID: q6r7s8t9u0v1
Revises: p5q6r7s8t9u0
Create Date: 2026-05-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "q6r7s8t9u0v1"
down_revision: Union[str, None] = "p5q6r7s8t9u0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRIGGER_ENUM_NAME = "consensus_trigger_enum"
TRIGGER_VALUES = ("BOARD_APPROVED", "MANUAL", "SCHEDULED")


def upgrade() -> None:
    trigger_enum = postgresql.ENUM(
        *TRIGGER_VALUES, name=TRIGGER_ENUM_NAME, create_type=False
    )
    trigger_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "consensus_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("draft_year", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("num_boards", sa.Integer(), nullable=False),
        sa.Column(
            "board_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("trigger", trigger_enum, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_consensus_snapshots_draft_year", "consensus_snapshots", ["draft_year"]
    )
    op.create_index(
        "ix_consensus_snapshots_computed_at",
        "consensus_snapshots",
        ["computed_at"],
    )
    op.create_index(
        "ix_consensus_snapshots_year_computed",
        "consensus_snapshots",
        ["draft_year", "computed_at"],
    )

    op.create_table(
        "big_board_consensus",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("draft_year", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("consensus_rank", sa.Integer(), nullable=False),
        sa.Column("avg_rank", sa.Float(), nullable=False),
        sa.Column("median_rank", sa.Float(), nullable=False),
        sa.Column("high_rank", sa.Integer(), nullable=False),
        sa.Column("low_rank", sa.Integer(), nullable=False),
        sa.Column("std_dev", sa.Float(), nullable=False),
        sa.Column("num_sources", sa.Integer(), nullable=False),
        sa.Column("prev_rank", sa.Integer(), nullable=True),
        sa.Column("rank_delta", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["consensus_snapshots.id"],
            name="fk_big_board_consensus_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players_master.id"],
            name="fk_big_board_consensus_player",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "player_id",
            name="uq_big_board_consensus_snapshot_player",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "consensus_rank",
            name="uq_big_board_consensus_snapshot_rank",
        ),
    )
    op.create_index(
        "ix_big_board_consensus_draft_year",
        "big_board_consensus",
        ["draft_year"],
    )
    op.create_index(
        "ix_big_board_consensus_player_id",
        "big_board_consensus",
        ["player_id"],
    )
    op.create_index(
        "ix_big_board_consensus_snapshot_rank",
        "big_board_consensus",
        ["snapshot_id", "consensus_rank"],
    )
    op.create_index(
        "ix_big_board_consensus_player_snapshot",
        "big_board_consensus",
        ["player_id", "snapshot_id"],
    )

    op.create_table(
        "source_analytics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("news_source_id", sa.Integer(), nullable=False),
        sa.Column("latest_board_id", sa.Integer(), nullable=False),
        sa.Column("avg_deviation", sa.Float(), nullable=False),
        sa.Column("contrarian_score", sa.Float(), nullable=False),
        sa.Column("biggest_outlier_player_id", sa.Integer(), nullable=True),
        sa.Column(
            "outlier_delta",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["consensus_snapshots.id"],
            name="fk_source_analytics_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_source_id"],
            ["news_sources.id"],
            name="fk_source_analytics_news_source",
        ),
        sa.ForeignKeyConstraint(
            ["latest_board_id"],
            ["big_boards.id"],
            name="fk_source_analytics_latest_board",
        ),
        sa.ForeignKeyConstraint(
            ["biggest_outlier_player_id"],
            ["players_master.id"],
            name="fk_source_analytics_biggest_outlier_player",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "news_source_id",
            name="uq_source_analytics_snapshot_source",
        ),
    )
    op.create_index(
        "ix_source_analytics_news_source_id",
        "source_analytics",
        ["news_source_id"],
    )
    op.create_index(
        "ix_source_analytics_snapshot",
        "source_analytics",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_analytics_snapshot", table_name="source_analytics")
    op.drop_index(
        "ix_source_analytics_news_source_id", table_name="source_analytics"
    )
    op.drop_table("source_analytics")

    op.drop_index(
        "ix_big_board_consensus_player_snapshot", table_name="big_board_consensus"
    )
    op.drop_index(
        "ix_big_board_consensus_snapshot_rank", table_name="big_board_consensus"
    )
    op.drop_index("ix_big_board_consensus_player_id", table_name="big_board_consensus")
    op.drop_index("ix_big_board_consensus_draft_year", table_name="big_board_consensus")
    op.drop_table("big_board_consensus")

    op.drop_index(
        "ix_consensus_snapshots_year_computed", table_name="consensus_snapshots"
    )
    op.drop_index(
        "ix_consensus_snapshots_computed_at", table_name="consensus_snapshots"
    )
    op.drop_index(
        "ix_consensus_snapshots_draft_year", table_name="consensus_snapshots"
    )
    op.drop_table("consensus_snapshots")

    bind = op.get_bind()
    sa.Enum(name=TRIGGER_ENUM_NAME).drop(bind, checkfirst=True)

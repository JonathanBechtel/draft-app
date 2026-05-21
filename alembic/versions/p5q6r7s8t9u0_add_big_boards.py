"""Add big_boards and big_board_entries tables.

Revision ID: p5q6r7s8t9u0
Revises: o4p5q6r7s8t9
Create Date: 2026-05-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "p5q6r7s8t9u0"
down_revision: Union[str, None] = "o4p5q6r7s8t9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BOARD_STATUS_ENUM_NAME = "board_status_enum"
BOARD_STATUS_VALUES = ("PENDING", "APPROVED", "REJECTED")


def upgrade() -> None:
    board_status_enum = postgresql.ENUM(
        *BOARD_STATUS_VALUES, name=BOARD_STATUS_ENUM_NAME, create_type=False
    )
    board_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "big_boards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("news_source_id", sa.Integer(), nullable=False),
        sa.Column("news_item_id", sa.Integer(), nullable=True),
        sa.Column("draft_year", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("board_size", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            board_status_enum,
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["news_source_id"],
            ["news_sources.id"],
            name="fk_big_boards_news_source",
        ),
        sa.ForeignKeyConstraint(
            ["news_item_id"],
            ["news_items.id"],
            name="fk_big_boards_news_item",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_big_boards_news_source_id", "big_boards", ["news_source_id"])
    op.create_index("ix_big_boards_draft_year", "big_boards", ["draft_year"])
    op.create_index("ix_big_boards_published_at", "big_boards", ["published_at"])
    op.create_index("ix_big_boards_status", "big_boards", ["status"])
    op.create_index(
        "ix_big_boards_status_draft_year", "big_boards", ["status", "draft_year"]
    )
    op.create_index(
        "ix_big_boards_source_draft_year",
        "big_boards",
        ["news_source_id", "draft_year"],
    )

    op.create_table(
        "big_board_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["board_id"],
            ["big_boards.id"],
            name="fk_big_board_entries_board",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players_master.id"],
            name="fk_big_board_entries_player",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("board_id", "rank", name="uq_big_board_entries_board_rank"),
        sa.UniqueConstraint(
            "board_id", "player_id", name="uq_big_board_entries_board_player"
        ),
    )
    op.create_index("ix_big_board_entries_board_id", "big_board_entries", ["board_id"])
    op.create_index("ix_big_board_entries_player", "big_board_entries", ["player_id"])


def downgrade() -> None:
    op.drop_index("ix_big_board_entries_player", table_name="big_board_entries")
    op.drop_index("ix_big_board_entries_board_id", table_name="big_board_entries")
    op.drop_table("big_board_entries")

    op.drop_index("ix_big_boards_source_draft_year", table_name="big_boards")
    op.drop_index("ix_big_boards_status_draft_year", table_name="big_boards")
    op.drop_index("ix_big_boards_status", table_name="big_boards")
    op.drop_index("ix_big_boards_published_at", table_name="big_boards")
    op.drop_index("ix_big_boards_draft_year", table_name="big_boards")
    op.drop_index("ix_big_boards_news_source_id", table_name="big_boards")
    op.drop_table("big_boards")

    bind = op.get_bind()
    sa.Enum(name=BOARD_STATUS_ENUM_NAME).drop(bind, checkfirst=True)

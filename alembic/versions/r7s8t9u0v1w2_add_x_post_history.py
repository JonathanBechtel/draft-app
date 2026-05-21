"""Add x_post_history table for the X thread skill.

Revision ID: r7s8t9u0v1w2
Revises: q6r7s8t9u0v1
Create Date: 2026-05-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "r7s8t9u0v1w2"
down_revision: Union[str, None] = "q6r7s8t9u0v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ANGLE_ENUM_NAME = "x_post_angle_enum"
STATUS_ENUM_NAME = "x_post_status_enum"
ANGLE_VALUES = ("spotlight", "h2h", "outlier", "consensus_shift", "news_tag")
STATUS_VALUES = ("draft", "posted", "skipped")


def upgrade() -> None:
    angle_enum = postgresql.ENUM(*ANGLE_VALUES, name=ANGLE_ENUM_NAME, create_type=False)
    status_enum = postgresql.ENUM(
        *STATUS_VALUES, name=STATUS_ENUM_NAME, create_type=False
    )
    angle_enum.create(op.get_bind(), checkfirst=True)
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "x_post_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("angle", angle_enum, nullable=False),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "player_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("news_item_id", sa.Integer(), nullable=True),
        sa.Column("headline", sa.String(), nullable=True),
        sa.Column(
            "tweets",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "image_paths",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("draft_dir", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("external_post_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["news_item_id"], ["news_items.id"], name="fk_x_post_history_news_item"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_x_post_history_angle_created", "x_post_history", ["angle", "created_at"]
    )
    op.create_index(
        "ix_x_post_history_status_created", "x_post_history", ["status", "created_at"]
    )
    op.create_index("ix_x_post_history_created_at", "x_post_history", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_x_post_history_created_at", table_name="x_post_history")
    op.drop_index("ix_x_post_history_status_created", table_name="x_post_history")
    op.drop_index("ix_x_post_history_angle_created", table_name="x_post_history")
    op.drop_table("x_post_history")

    bind = op.get_bind()
    sa.Enum(name=STATUS_ENUM_NAME).drop(bind, checkfirst=True)
    sa.Enum(name=ANGLE_ENUM_NAME).drop(bind, checkfirst=True)

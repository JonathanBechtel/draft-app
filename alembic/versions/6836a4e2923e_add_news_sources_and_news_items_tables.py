"""add news_sources and news_items tables

Revision ID: 6836a4e2923e
Revises: b2c3d4e5f6a7
Create Date: 2025-12-31 01:05:49.126467
"""

import sqlmodel.sql.sqltypes
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa

revision = "6836a4e2923e"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "news_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("display_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("feed_type", sa.Enum("RSS", name="feedtype"), nullable=False),
        sa.Column("feed_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("fetch_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_url"),
    )
    op.create_index(
        op.f("ix_news_sources_is_active"), "news_sources", ["is_active"], unique=False
    )
    op.create_index(
        op.f("ix_news_sources_name"), "news_sources", ["name"], unique=False
    )

    op.create_table(
        "news_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("image_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("author", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("summary", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "tag",
            sa.Enum("RISER", "FALLER", "ANALYSIS", "HIGHLIGHT", name="newsitemtag"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players_master.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["news_sources.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "external_id", name="uq_news_items_source_external"
        ),
    )
    op.create_index(
        op.f("ix_news_items_external_id"), "news_items", ["external_id"], unique=False
    )
    op.create_index(
        op.f("ix_news_items_published_at"),
        "news_items",
        ["published_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_news_items_source_id"), "news_items", ["source_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_news_items_source_id"), table_name="news_items")
    op.drop_index(op.f("ix_news_items_published_at"), table_name="news_items")
    op.drop_index(op.f("ix_news_items_external_id"), table_name="news_items")
    op.drop_table("news_items")

    op.drop_index(op.f("ix_news_sources_name"), table_name="news_sources")
    op.drop_index(op.f("ix_news_sources_is_active"), table_name="news_sources")
    op.drop_table("news_sources")

    # Drop the enum types
    sa.Enum(name="newsitemtag").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="feedtype").drop(op.get_bind(), checkfirst=True)

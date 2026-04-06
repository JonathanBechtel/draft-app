"""Merge legacy preview-app revision into the current migration chain.

Revision ID: c9705695210b
Revises: 327ae506058d, b9705695210a
Create Date: 2026-04-06 11:20:00.000000
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy import text
import sqlmodel.sql.sqltypes

revision = "c9705695210b"
down_revision = ("327ae506058d", "b9705695210a")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Converge orphaned preview DBs onto the canonical March 19 schema."""

    conn = op.get_bind()

    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'players_master' AND column_name = 'rsci_rank'"
        )
    )
    if not result.fetchone():
        op.add_column(
            "players_master",
            sa.Column("rsci_rank", sa.Integer(), nullable=True),
        )

    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'player_college_stats'"
        )
    )
    if not result.fetchone():
        op.create_table(
            "player_college_stats",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("player_id", sa.Integer(), sa.ForeignKey("players_master.id"), nullable=False),
            sa.Column("season", sa.String(), nullable=False),
            sa.Column("games", sa.Integer(), nullable=True),
            sa.Column("games_started", sa.Integer(), nullable=True),
            sa.Column("mpg", sa.Float(), nullable=True),
            sa.Column("ppg", sa.Float(), nullable=True),
            sa.Column("rpg", sa.Float(), nullable=True),
            sa.Column("apg", sa.Float(), nullable=True),
            sa.Column("spg", sa.Float(), nullable=True),
            sa.Column("bpg", sa.Float(), nullable=True),
            sa.Column("tov", sa.Float(), nullable=True),
            sa.Column("pf", sa.Float(), nullable=True),
            sa.Column("fg_pct", sa.Float(), nullable=True),
            sa.Column("three_p_pct", sa.Float(), nullable=True),
            sa.Column("three_pa", sa.Float(), nullable=True),
            sa.Column("ft_pct", sa.Float(), nullable=True),
            sa.Column("fta", sa.Float(), nullable=True),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("player_id", "season", name="uq_college_stats_player_season"),
        )
        op.create_index("ix_player_college_stats_player_id", "player_college_stats", ["player_id"])
        op.create_index("ix_player_college_stats_season", "player_college_stats", ["season"])

    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'players_master' AND column_name = 'bio_source'"
        )
    )
    if not result.fetchone():
        op.add_column(
            "players_master",
            sa.Column("bio_source", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )

    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'players_master' AND column_name = 'enrichment_attempted_at'"
        )
    )
    if not result.fetchone():
        op.add_column(
            "players_master",
            sa.Column("enrichment_attempted_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade is intentionally a no-op for this compatibility bridge."""

"""Add index on summer_league_games.round_label.

Revision ID: bb20c6f83560
Revises: 6bcc10685511
Create Date: 2026-06-22

"""

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "bb20c6f83560"
down_revision: str = "6bcc10685511"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: on a from-scratch upgrade b6c7d8e9f0a1's create_all already
    # materialized this index (round_label now has index=True in SQLModel).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_summer_league_games_round_label"
        " ON summer_league_games (round_label)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_summer_league_games_round_label",
        table_name="summer_league_games",
        if_exists=True,
    )

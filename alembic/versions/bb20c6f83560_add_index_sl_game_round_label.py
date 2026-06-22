"""Add index on summer_league_games.round_label.

Revision ID: bb20c6f83560
Revises: 1233bc724a9f
Create Date: 2026-06-22

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb20c6f83560"
down_revision: str = "1233bc724a9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_summer_league_games_round_label",
        "summer_league_games",
        ["round_label"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_summer_league_games_round_label",
        table_name="summer_league_games",
    )

"""Add summer_league_games scoreboard fields: raw team IDs + honest status text.

Existing-table migration (#529 "scoreboard as canonical schedule source"):
scoreboard ingest now retains the provider's raw home/away NBA Stats team IDs
(independent of the resolved *_team_entry_id FKs, so an unresolved provider
team ID can still be reported instead of silently dropped) and the honest raw
`gameStatusText` (e.g. "Final/OT", "PPD") alongside the coarse `status`
enum bucket. Never drops/recreates summer_league_games -- adds three nullable
columns.

Revision ID: a2b3c4d5e6f7
Revises: 77f1f90be54a
Create Date: 2026-07-10
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "77f1f90be54a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable home/away raw provider team ID columns + status_text."""
    # Idempotent: b6c7d8e9f0a1 creates summer_league_games via
    # SQLModel.metadata.create_all, which on a from-scratch upgrade now
    # reflects these columns and creates them already. On an existing DB the
    # columns are absent and this adds them. IF NOT EXISTS keeps both paths
    # green (matches the precedent set by b3d9f17c2a84 / e7c75f3063ec for
    # this same table).
    op.execute(
        "ALTER TABLE summer_league_games "
        "ADD COLUMN IF NOT EXISTS home_nba_stats_team_id VARCHAR"
    )
    op.execute(
        "ALTER TABLE summer_league_games "
        "ADD COLUMN IF NOT EXISTS away_nba_stats_team_id VARCHAR"
    )
    op.execute(
        "ALTER TABLE summer_league_games ADD COLUMN IF NOT EXISTS status_text VARCHAR"
    )


def downgrade() -> None:
    """Drop the three columns added above."""
    op.execute(
        "ALTER TABLE summer_league_games DROP COLUMN IF EXISTS home_nba_stats_team_id"
    )
    op.execute(
        "ALTER TABLE summer_league_games DROP COLUMN IF EXISTS away_nba_stats_team_id"
    )
    op.execute("ALTER TABLE summer_league_games DROP COLUMN IF EXISTS status_text")

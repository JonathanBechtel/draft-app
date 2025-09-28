"""Adopt short codes for player_position enum.

Revision ID: 20250928_000002
Revises: 20240924_000001
Create Date: 2025-09-28 00:00:02
"""

from alembic import op


revision = "20250928_000002"
down_revision = "20240924_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE player_position_enum RENAME TO player_position_enum_old")
    op.execute("CREATE TYPE player_position_enum AS ENUM ('g', 'f', 'c')")
    op.execute(
        """
        ALTER TABLE players
        ALTER COLUMN player_position
        TYPE player_position_enum
        USING (
            CASE player_position
                WHEN 'guard' THEN 'g'::player_position_enum
                WHEN 'forward' THEN 'f'::player_position_enum
                WHEN 'center' THEN 'c'::player_position_enum
                ELSE player_position::text::player_position_enum
            END
        )
        """
    )
    op.execute("DROP TYPE player_position_enum_old")


def downgrade() -> None:
    op.execute("ALTER TYPE player_position_enum RENAME TO player_position_enum_new")
    op.execute("CREATE TYPE player_position_enum AS ENUM ('guard', 'forward', 'center')")
    op.execute(
        """
        ALTER TABLE players
        ALTER COLUMN player_position
        TYPE player_position_enum
        USING (
            CASE player_position
                WHEN 'g' THEN 'guard'::player_position_enum
                WHEN 'f' THEN 'forward'::player_position_enum
                WHEN 'c' THEN 'center'::player_position_enum
                ELSE player_position::text::player_position_enum
            END
        )
        """
    )
    op.execute("DROP TYPE player_position_enum_new")

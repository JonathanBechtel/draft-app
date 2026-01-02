"""update_newsitemtag_enum_values

Revision ID: 95dcddec45ad
Revises: 6836a4e2923e
Create Date: 2026-01-02 00:46:42.658069

Updates the newsitemtag PostgreSQL enum to match the new Python enum values.
"""

from alembic import op  # type: ignore[attr-defined]

revision = "95dcddec45ad"
down_revision = "6836a4e2923e"
branch_labels = None
depends_on = None

# New enum values (Python enum names)
NEW_ENUM_VALUES = [
    "SCOUTING_REPORT",
    "BIG_BOARD",
    "MOCK_DRAFT",
    "TIER_UPDATE",
    "GAME_RECAP",
    "FILM_STUDY",
    "SKILL_THEME",
    "TEAM_FIT",
    "DRAFT_INTEL",
    "STATS_ANALYSIS",
]

# Old enum values
OLD_ENUM_VALUES = ["RISER", "FALLER", "ANALYSIS", "HIGHLIGHT"]


def upgrade() -> None:
    # PostgreSQL doesn't allow modifying enum values directly
    # We need to: create new type, alter column, drop old type

    # 1. Clear existing news_items data (old tags won't map to new ones)
    op.execute("DELETE FROM news_items")

    # 2. Create the new enum type
    op.execute(
        f"CREATE TYPE newsitemtag_new AS ENUM ({', '.join(repr(v) for v in NEW_ENUM_VALUES)})"
    )

    # 3. Drop the default constraint first (required before type change)
    op.execute("ALTER TABLE news_items ALTER COLUMN tag DROP DEFAULT")

    # 4. Alter column to use the new enum type
    op.execute(
        "ALTER TABLE news_items "
        "ALTER COLUMN tag TYPE newsitemtag_new "
        "USING tag::text::newsitemtag_new"
    )

    # 5. Drop the old enum type
    op.execute("DROP TYPE newsitemtag")

    # 6. Rename new type to original name
    op.execute("ALTER TYPE newsitemtag_new RENAME TO newsitemtag")

    # 7. Set the new default
    op.execute(
        "ALTER TABLE news_items ALTER COLUMN tag SET DEFAULT 'SCOUTING_REPORT'::newsitemtag"
    )


def downgrade() -> None:
    # Clear data and revert to old enum
    op.execute("DELETE FROM news_items")

    op.execute(
        f"CREATE TYPE newsitemtag_old AS ENUM ({', '.join(repr(v) for v in OLD_ENUM_VALUES)})"
    )

    op.execute("ALTER TABLE news_items ALTER COLUMN tag DROP DEFAULT")

    op.execute(
        "ALTER TABLE news_items "
        "ALTER COLUMN tag TYPE newsitemtag_old "
        "USING 'ANALYSIS'::newsitemtag_old"  # Default to ANALYSIS for any data
    )

    op.execute("DROP TYPE newsitemtag")
    op.execute("ALTER TYPE newsitemtag_old RENAME TO newsitemtag")
    op.execute(
        "ALTER TABLE news_items ALTER COLUMN tag SET DEFAULT 'RISER'::newsitemtag"
    )

"""Enforce canonical enum-backed storage for content classification columns.

Revision ID: k0l1m2n3o4p5
Revises: j9k0l1m2n3o4
Create Date: 2026-02-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "k0l1m2n3o4p5"
down_revision: Union[str, None] = "j9k0l1m2n3o4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEWS_TAG_NAMES = (
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
)

PODCAST_TAG_NAMES = (
    "INTERVIEW",
    "DRAFT_ANALYSIS",
    "MOCK_DRAFT",
    "GAME_BREAKDOWN",
    "TRADE_INTEL",
    "PROSPECT_DEBATE",
    "MAILBAG",
    "EVENT_PREVIEW",
)

CONTENT_TYPE_NAMES = ("NEWS", "PODCAST")
MENTION_SOURCE_NAMES = ("AI", "BACKFILL", "MANUAL")


def _raise_if_invalid_values(
    bind: sa.engine.Connection,
    *,
    table: str,
    column: str,
    allowed_values: tuple[str, ...],
) -> None:
    allowed_sql = ", ".join(f"'{v}'" for v in allowed_values)
    stmt = sa.text(
        f"""
        SELECT DISTINCT {column}::text
        FROM {table}
        WHERE {column} IS NULL OR {column}::text NOT IN ({allowed_sql})
        ORDER BY {column}::text
        """
    )
    invalid_values = list(bind.execute(stmt).scalars().all())
    if invalid_values:
        raise RuntimeError(
            f"Invalid values remain in {table}.{column}: {invalid_values}"
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Ensure required enum types exist before column type conversion.
    postgresql.ENUM(*NEWS_TAG_NAMES, name="newsitemtag").create(bind, checkfirst=True)
    postgresql.ENUM(*PODCAST_TAG_NAMES, name="podcastepisodetag").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*CONTENT_TYPE_NAMES, name="contenttype").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*MENTION_SOURCE_NAMES, name="mentionsource").create(
        bind, checkfirst=True
    )

    # Normalize news tags from display/legacy forms to canonical enum names.
    op.execute(
        """
        UPDATE news_items
        SET tag = CASE lower(tag)
            WHEN 'scouting report' THEN 'SCOUTING_REPORT'
            WHEN 'big board' THEN 'BIG_BOARD'
            WHEN 'mock draft' THEN 'MOCK_DRAFT'
            WHEN 'tier update' THEN 'TIER_UPDATE'
            WHEN 'game recap' THEN 'GAME_RECAP'
            WHEN 'film study' THEN 'FILM_STUDY'
            WHEN 'skill theme' THEN 'SKILL_THEME'
            WHEN 'team fit' THEN 'TEAM_FIT'
            WHEN 'draft intel' THEN 'DRAFT_INTEL'
            WHEN 'statistical analysis' THEN 'STATS_ANALYSIS'
            ELSE upper(tag)
        END
        WHERE tag IS NOT NULL
        """
    )

    # Normalize podcast tags from display/legacy forms to canonical enum names.
    op.execute(
        """
        UPDATE podcast_episodes
        SET tag = CASE lower(tag)
            WHEN 'interview' THEN 'INTERVIEW'
            WHEN 'draft analysis' THEN 'DRAFT_ANALYSIS'
            WHEN 'mock draft' THEN 'MOCK_DRAFT'
            WHEN 'game breakdown' THEN 'GAME_BREAKDOWN'
            WHEN 'trade & intel' THEN 'TRADE_INTEL'
            WHEN 'prospect debate' THEN 'PROSPECT_DEBATE'
            WHEN 'mailbag' THEN 'MAILBAG'
            WHEN 'event preview' THEN 'EVENT_PREVIEW'
            ELSE upper(tag)
        END
        WHERE tag IS NOT NULL
        """
    )

    # Normalize mention columns from mixed-case text to canonical enum names.
    op.execute(
        """
        UPDATE player_content_mentions
        SET content_type = upper(content_type)
        WHERE content_type IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE player_content_mentions
        SET source = upper(source)
        WHERE source IS NOT NULL
        """
    )

    # Fail fast before tightening schema if unexpected values remain.
    _raise_if_invalid_values(
        bind,
        table="news_items",
        column="tag",
        allowed_values=NEWS_TAG_NAMES,
    )
    _raise_if_invalid_values(
        bind,
        table="podcast_episodes",
        column="tag",
        allowed_values=PODCAST_TAG_NAMES,
    )
    _raise_if_invalid_values(
        bind,
        table="player_content_mentions",
        column="content_type",
        allowed_values=CONTENT_TYPE_NAMES,
    )
    _raise_if_invalid_values(
        bind,
        table="player_content_mentions",
        column="source",
        allowed_values=MENTION_SOURCE_NAMES,
    )

    # Enforce enum-backed storage.
    op.execute("ALTER TABLE news_items ALTER COLUMN tag DROP DEFAULT")
    op.execute(
        "ALTER TABLE news_items "
        "ALTER COLUMN tag TYPE newsitemtag "
        "USING tag::text::newsitemtag"
    )
    op.execute(
        "ALTER TABLE news_items "
        "ALTER COLUMN tag SET DEFAULT 'SCOUTING_REPORT'::newsitemtag"
    )
    op.execute("ALTER TABLE news_items ALTER COLUMN tag SET NOT NULL")

    op.execute("ALTER TABLE podcast_episodes ALTER COLUMN tag DROP DEFAULT")
    op.execute(
        "ALTER TABLE podcast_episodes "
        "ALTER COLUMN tag TYPE podcastepisodetag "
        "USING tag::text::podcastepisodetag"
    )
    op.execute(
        "ALTER TABLE podcast_episodes "
        "ALTER COLUMN tag SET DEFAULT 'DRAFT_ANALYSIS'::podcastepisodetag"
    )

    op.execute(
        "ALTER TABLE player_content_mentions "
        "ALTER COLUMN content_type TYPE contenttype "
        "USING content_type::text::contenttype"
    )
    op.execute(
        "ALTER TABLE player_content_mentions "
        "ALTER COLUMN source TYPE mentionsource "
        "USING source::text::mentionsource"
    )


def downgrade() -> None:
    # Return columns to string-backed storage used before this migration.
    op.execute("ALTER TABLE news_items ALTER COLUMN tag DROP DEFAULT")
    op.execute(
        "ALTER TABLE news_items "
        "ALTER COLUMN tag TYPE VARCHAR "
        "USING tag::text"
    )
    op.execute("ALTER TABLE news_items ALTER COLUMN tag DROP NOT NULL")
    op.execute(
        "ALTER TABLE news_items "
        "ALTER COLUMN tag SET DEFAULT 'SCOUTING_REPORT'::character varying"
    )

    op.execute("ALTER TABLE podcast_episodes ALTER COLUMN tag DROP DEFAULT")
    op.execute(
        "ALTER TABLE podcast_episodes "
        "ALTER COLUMN tag TYPE VARCHAR "
        "USING tag::text"
    )
    op.execute(
        "ALTER TABLE podcast_episodes "
        "ALTER COLUMN tag SET DEFAULT 'DRAFT_ANALYSIS'::character varying"
    )

    op.execute(
        "ALTER TABLE player_content_mentions "
        "ALTER COLUMN content_type TYPE VARCHAR "
        "USING lower(content_type::text)"
    )
    op.execute(
        "ALTER TABLE player_content_mentions "
        "ALTER COLUMN source TYPE VARCHAR "
        "USING lower(source::text)"
    )

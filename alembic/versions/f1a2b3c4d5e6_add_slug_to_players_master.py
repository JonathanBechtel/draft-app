"""add slug to players_master

Revision ID: f1a2b3c4d5e6
Revises: e4c5e5f4a2c3
Create Date: 2025-11-30 12:00:00.000000
"""

import re
import unicodedata
from alembic import op  # type: ignore[attr-defined]
import sqlmodel
import sqlalchemy as sa
from sqlalchemy import text

revision = "f1a2b3c4d5e6"
down_revision = "e4c5e5f4a2c3"
branch_labels = None
depends_on = None


def generate_slug(name: str) -> str:
    """Convert a display name to a URL-safe slug."""
    if not name:
        return ""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lower = ascii_text.lower()
    hyphenated = re.sub(r"[\s_]+", "-", lower)
    cleaned = re.sub(r"[^a-z0-9-]", "", hyphenated)
    collapsed = re.sub(r"-+", "-", cleaned)
    return collapsed.strip("-")


def upgrade() -> None:
    # Add slug column (nullable initially for backfill)
    op.add_column(
        "players_master",
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )

    # Backfill slugs for existing players
    conn = op.get_bind()
    players = conn.execute(
        text("SELECT id, display_name FROM players_master ORDER BY id")
    ).fetchall()

    used_slugs: set[str] = set()
    for player_id, display_name in players:
        base_slug = generate_slug(display_name or "")
        if not base_slug:
            base_slug = "player"

        slug = base_slug
        suffix = 1
        while slug in used_slugs:
            suffix += 1
            slug = f"{base_slug}-{suffix}"

        used_slugs.add(slug)
        conn.execute(
            text("UPDATE players_master SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": player_id},
        )

    # Create unique index after backfill
    op.create_index(
        op.f("ix_players_master_slug"),
        "players_master",
        ["slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_players_master_slug"), table_name="players_master")
    op.drop_column("players_master", "slug")

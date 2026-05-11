"""Add career_status to player_lifecycle.

Revision ID: e6f7g8h9i0j1
Revises: 35d4c25324b3
Create Date: 2026-05-11
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e6f7g8h9i0j1"
down_revision = "35d4c25324b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill explicit career status taxonomy."""
    bind = op.get_bind()
    career_status_enum = postgresql.ENUM(
        "ACTIVE",
        "FREE_AGENT",
        "PROSPECT",
        "G_LEAGUE",
        "OVERSEAS",
        "RETIRED",
        "UNDRAFTED",
        "UNKNOWN",
        name="career_status_enum",
        create_type=False,
    )
    career_status_enum.create(bind, checkfirst=True)

    op.add_column(
        "player_lifecycle",
        sa.Column(
            "career_status",
            career_status_enum,
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    op.create_index(
        op.f("ix_player_lifecycle_career_status"),
        "player_lifecycle",
        ["career_status"],
        unique=False,
    )

    op.execute(
        """
        UPDATE player_lifecycle pl
        SET career_status = (
            CASE
                WHEN ps.is_active_nba IS TRUE
                    OR pl.lifecycle_stage = 'NBA_ACTIVE' THEN 'ACTIVE'
                WHEN pl.competition_context = 'G_LEAGUE' THEN 'G_LEAGUE'
                WHEN pl.competition_context = 'OVERSEAS_PRO'
                    OR pl.lifecycle_stage = 'PRO_NON_NBA' THEN 'OVERSEAS'
                WHEN pl.lifecycle_stage = 'INACTIVE_FORMER' THEN 'RETIRED'
                WHEN pl.draft_status = 'UNDRAFTED' THEN 'UNDRAFTED'
                WHEN pl.lifecycle_stage IN (
                    'RECRUIT',
                    'HIGH_SCHOOL',
                    'COLLEGE',
                    'INTERNATIONAL_AMATEUR',
                    'DRAFT_DECLARED',
                    'DRAFT_WITHDREW'
                )
                    OR pl.is_draft_prospect IS TRUE THEN 'PROSPECT'
                WHEN pl.lifecycle_stage = 'DRAFTED_NOT_IN_NBA' THEN 'FREE_AGENT'
                ELSE 'UNKNOWN'
            END
        )::career_status_enum
        FROM players_master pm
        LEFT JOIN player_status ps ON ps.player_id = pm.id
        WHERE pl.player_id = pm.id
        """
    )


def downgrade() -> None:
    """Remove career status taxonomy."""
    op.drop_index(
        op.f("ix_player_lifecycle_career_status"),
        table_name="player_lifecycle",
    )
    op.drop_column("player_lifecycle", "career_status")

    bind = op.get_bind()
    postgresql.ENUM(name="career_status_enum").drop(bind, checkfirst=True)

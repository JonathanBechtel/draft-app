"""Add player_lifecycle table.

Revision ID: 6a7b8c9d0e1f
Revises: f5g6h7i8j9k0
Create Date: 2026-04-05
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
import sqlmodel


revision = "6a7b8c9d0e1f"
down_revision = "f5g6h7i8j9k0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create lifecycle table and conservatively backfill current state."""
    op.create_table(
        "player_lifecycle",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column(
            "lifecycle_stage",
            sa.Enum(
                "RECRUIT",
                "HIGH_SCHOOL",
                "COLLEGE",
                "INTERNATIONAL_AMATEUR",
                "DRAFT_DECLARED",
                "DRAFT_WITHDREW",
                "DRAFTED_NOT_IN_NBA",
                "NBA_ACTIVE",
                "PRO_NON_NBA",
                "INACTIVE_FORMER",
                "UNKNOWN",
                name="player_lifecycle_stage_enum",
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column(
            "competition_context",
            sa.Enum(
                "HIGH_SCHOOL",
                "NCAA",
                "INTERNATIONAL",
                "NBA",
                "G_LEAGUE",
                "OVERSEAS_PRO",
                "INACTIVE",
                "UNKNOWN",
                name="competition_context_enum",
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column(
            "draft_status",
            sa.Enum(
                "NOT_ELIGIBLE",
                "ELIGIBLE",
                "DECLARED",
                "WITHDREW",
                "DRAFTED",
                "UNDRAFTED",
                "UNKNOWN",
                name="draft_status_enum",
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("expected_draft_year", sa.Integer(), nullable=True),
        sa.Column(
            "current_affiliation_name",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "current_affiliation_type",
            sa.Enum(
                "HIGH_SCHOOL",
                "COLLEGE_TEAM",
                "COMMITTED_SCHOOL",
                "NBA_TEAM",
                "G_LEAGUE_TEAM",
                "OVERSEAS_CLUB",
                "INDEPENDENT",
                "UNKNOWN",
                name="affiliation_type_enum",
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column(
            "commitment_school",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "commitment_status",
            sa.Enum(
                "COMMITTED",
                "SIGNED",
                "ENROLLED",
                "DECOMMITTED",
                "NONE",
                "UNKNOWN",
                name="commitment_status_enum",
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("is_draft_prospect", sa.Boolean(), nullable=True),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["player_id"], ["players_master.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("player_id", name="uq_player_lifecycle_player"),
    )
    op.create_index(
        op.f("ix_player_lifecycle_player_id"),
        "player_lifecycle",
        ["player_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_lifecycle_expected_draft_year"),
        "player_lifecycle",
        ["expected_draft_year"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_lifecycle_current_affiliation_name"),
        "player_lifecycle",
        ["current_affiliation_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_lifecycle_commitment_school"),
        "player_lifecycle",
        ["commitment_school"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_lifecycle_is_draft_prospect"),
        "player_lifecycle",
        ["is_draft_prospect"],
        unique=False,
    )

    op.execute(
        """
        INSERT INTO player_lifecycle (
            player_id,
            lifecycle_stage,
            competition_context,
            draft_status,
            expected_draft_year,
            current_affiliation_name,
            current_affiliation_type,
            commitment_school,
            commitment_status,
            is_draft_prospect,
            source,
            confidence,
            updated_at
        )
        SELECT
            pm.id,
            (
                CASE
                    WHEN ps.is_active_nba IS TRUE THEN 'NBA_ACTIVE'
                    WHEN pm.draft_year IS NOT NULL
                         AND NOT (
                             pm.is_stub IS TRUE
                             AND pm.draft_round IS NULL
                             AND pm.draft_pick IS NULL
                             AND pm.draft_team IS NULL
                             AND pm.nba_debut_date IS NULL
                             AND pm.nba_debut_season IS NULL
                         )
                    THEN 'DRAFTED_NOT_IN_NBA'
                    WHEN pm.school IS NOT NULL THEN 'COLLEGE'
                    WHEN pm.high_school IS NOT NULL THEN 'HIGH_SCHOOL'
                    ELSE 'UNKNOWN'
                END
            )::player_lifecycle_stage_enum,
            (
                CASE
                    WHEN ps.is_active_nba IS TRUE THEN 'NBA'
                    WHEN pm.school IS NOT NULL THEN 'NCAA'
                    WHEN pm.high_school IS NOT NULL THEN 'HIGH_SCHOOL'
                    WHEN pm.draft_year IS NOT NULL THEN 'INACTIVE'
                    ELSE 'UNKNOWN'
                END
            )::competition_context_enum,
            (
                CASE
                    WHEN pm.draft_year IS NOT NULL
                         AND NOT (
                             pm.is_stub IS TRUE
                             AND pm.draft_round IS NULL
                             AND pm.draft_pick IS NULL
                             AND pm.draft_team IS NULL
                             AND pm.nba_debut_date IS NULL
                             AND pm.nba_debut_season IS NULL
                         )
                    THEN 'DRAFTED'
                    WHEN pm.draft_year IS NULL
                         AND pm.nba_debut_date IS NULL
                         AND pm.nba_debut_season IS NULL
                         AND COALESCE(ps.is_active_nba, FALSE) IS FALSE
                         AND (pm.school IS NOT NULL OR pm.high_school IS NOT NULL)
                    THEN 'ELIGIBLE'
                    ELSE 'UNKNOWN'
                END
            )::draft_status_enum,
            CASE
                WHEN pm.is_stub IS TRUE
                     AND pm.draft_year IS NOT NULL
                     AND pm.draft_round IS NULL
                     AND pm.draft_pick IS NULL
                     AND pm.draft_team IS NULL
                     AND pm.nba_debut_date IS NULL
                     AND pm.nba_debut_season IS NULL
                THEN pm.draft_year
                ELSE NULL
            END,
            CASE
                WHEN ps.current_team IS NOT NULL THEN ps.current_team
                WHEN pm.school IS NOT NULL THEN pm.school
                WHEN pm.high_school IS NOT NULL THEN pm.high_school
                ELSE NULL
            END,
            (
                CASE
                    WHEN ps.current_team IS NOT NULL AND ps.is_active_nba IS TRUE THEN 'NBA_TEAM'
                    WHEN pm.school IS NOT NULL THEN 'COLLEGE_TEAM'
                    WHEN pm.high_school IS NOT NULL THEN 'HIGH_SCHOOL'
                    ELSE 'UNKNOWN'
                END
            )::affiliation_type_enum,
            NULL,
            'UNKNOWN'::commitment_status_enum,
            CASE
                WHEN pm.is_stub IS TRUE
                     AND pm.draft_year IS NOT NULL
                     AND pm.draft_round IS NULL
                     AND pm.draft_pick IS NULL
                     AND pm.draft_team IS NULL
                THEN TRUE
                WHEN pm.draft_year IS NULL
                     AND pm.nba_debut_date IS NULL
                     AND pm.nba_debut_season IS NULL
                     AND COALESCE(ps.is_active_nba, FALSE) IS FALSE
                     AND (pm.school IS NOT NULL OR pm.high_school IS NOT NULL)
                THEN TRUE
                ELSE NULL
            END,
            'migration_backfill',
            NULL,
            NOW()
        FROM players_master pm
        LEFT JOIN player_status ps ON ps.player_id = pm.id
        """
    )


def downgrade() -> None:
    """Drop lifecycle table and enum types."""
    op.drop_index(
        op.f("ix_player_lifecycle_is_draft_prospect"), table_name="player_lifecycle"
    )
    op.drop_index(
        op.f("ix_player_lifecycle_commitment_school"), table_name="player_lifecycle"
    )
    op.drop_index(
        op.f("ix_player_lifecycle_current_affiliation_name"),
        table_name="player_lifecycle",
    )
    op.drop_index(
        op.f("ix_player_lifecycle_expected_draft_year"), table_name="player_lifecycle"
    )
    op.drop_index(op.f("ix_player_lifecycle_player_id"), table_name="player_lifecycle")
    op.drop_table("player_lifecycle")

    bind = op.get_bind()
    sa.Enum(name="commitment_status_enum").drop(bind, checkfirst=True)
    sa.Enum(name="affiliation_type_enum").drop(bind, checkfirst=True)
    sa.Enum(name="draft_status_enum").drop(bind, checkfirst=True)
    sa.Enum(name="competition_context_enum").drop(bind, checkfirst=True)
    sa.Enum(name="player_lifecycle_stage_enum").drop(bind, checkfirst=True)

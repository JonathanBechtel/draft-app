"""Add player_affiliations and summer_league_participation tables + participation_id column.

Workstream 0 schema foundation for 2026 Summer League rostering (ticket T1).

Creates:
  - player_affiliations: append-only, bitemporal affiliation assertions.
  - summer_league_participation: stable bridge per (player, team_entry, stint).
  - participation_id column + index on summer_league_player_game_logs (additive, nullable).

Two new PG enum types:
  - player_affiliation_type_enum (distinct from the existing affiliation_type_enum).
  - affiliation_status_enum (new, shared by both tables).

Revision ID: 2f09df4af11c
Revises: c0d1e2f3a4b5
Create Date: 2026-06-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

revision: str = "2f09df4af11c"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create both new tables and add participation_id to game logs."""
    bind = op.get_bind()

    # 1. Create the two new PG enum types explicitly (checkfirst guards against
    #    duplicate-type errors when upgrade is re-run or create_all ran first).
    affiliation_type_enum = postgresql.ENUM(
        "SUMMER_LEAGUE_ROSTER",
        "CLUB",
        "NATIONAL_TEAM",
        "NBA_CONTRACT",
        "COLLEGE",
        name="player_affiliation_type_enum",
        create_type=False,
    )
    affiliation_status_enum = postgresql.ENUM(
        "ANNOUNCED",
        "CONFIRMED",
        "ACTIVE",
        "CUT",
        "WITHDRAWN",
        name="affiliation_status_enum",
        create_type=False,
    )
    affiliation_type_enum.create(bind, checkfirst=True)
    affiliation_status_enum.create(bind, checkfirst=True)

    # 2. Create player_affiliations (append-only, bitemporal hub table).
    op.create_table(
        "player_affiliations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("nba_team_id", sa.Integer(), nullable=True),
        sa.Column(
            "affiliation_type",
            affiliation_type_enum,
            nullable=False,
        ),
        sa.Column(
            "status",
            affiliation_status_enum,
            nullable=False,
            server_default="ANNOUNCED",
        ),
        sa.Column("effective_start", sa.Date(), nullable=True),
        sa.Column("effective_end", sa.Date(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("retracted_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["nba_team_id"], ["nba_teams.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players_master.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["player_affiliations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_player_affiliations_player_id",
        "player_affiliations",
        ["player_id"],
        unique=False,
    )
    op.create_index(
        "ix_player_affiliations_nba_team_id",
        "player_affiliations",
        ["nba_team_id"],
        unique=False,
    )
    op.create_index(
        "ix_player_affiliations_status",
        "player_affiliations",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_player_affiliations_supersedes_id",
        "player_affiliations",
        ["supersedes_id"],
        unique=False,
    )
    op.create_index(
        "ix_player_affiliations_active",
        "player_affiliations",
        ["player_id", "nba_team_id"],
        unique=False,
        postgresql_where=sa.text("superseded_at IS NULL AND retracted_at IS NULL"),
    )

    # 3. Create summer_league_participation (stable stat bridge; FK → player_affiliations).
    op.create_table(
        "summer_league_participation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("team_entry_id", sa.Integer(), nullable=False),
        sa.Column("source_player_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("affiliation_id", sa.Integer(), nullable=True),
        sa.Column("stint_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "roster_status",
            affiliation_status_enum,
            nullable=False,
            server_default="ANNOUNCED",
        ),
        sa.Column("jersey_number", sa.String(), nullable=True),
        sa.Column("roster_position", sa.String(), nullable=True),
        sa.Column("first_game_date", sa.Date(), nullable=True),
        sa.Column("last_game_date", sa.Date(), nullable=True),
        sa.Column("games_played", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["affiliation_id"], ["player_affiliations.id"]),
        sa.ForeignKeyConstraint(
            ["competition_id"], ["summer_league_competitions.id"]
        ),
        sa.ForeignKeyConstraint(["player_id"], ["players_master.id"]),
        sa.ForeignKeyConstraint(
            ["source_player_id"], ["summer_league_source_players.id"]
        ),
        sa.ForeignKeyConstraint(
            ["team_entry_id"], ["summer_league_team_entries.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competition_id",
            "team_entry_id",
            "source_player_id",
            "stint_no",
            name="uq_summer_league_participation_comp_team_source_stint",
        ),
    )
    op.create_index(
        "ix_summer_league_participation_player_id",
        "summer_league_participation",
        ["player_id"],
        unique=False,
    )
    op.create_index(
        "ix_summer_league_participation_competition_team",
        "summer_league_participation",
        ["competition_id", "team_entry_id"],
        unique=False,
    )
    op.create_index(
        "ix_summer_league_participation_source_player_id",
        "summer_league_participation",
        ["source_player_id"],
        unique=False,
    )
    op.create_index(
        "ix_summer_league_participation_affiliation_id",
        "summer_league_participation",
        ["affiliation_id"],
        unique=False,
    )

    # 4. Add participation_id to summer_league_player_game_logs (additive, nullable).
    #    Pre-2026 rows remain NULL; 2026+ rows are backfilled by the loader.
    #    Soft reference (no DB-level FK): the game-log table is created by an earlier
    #    create_all() migration that reflects the live model, so a hard FK here would
    #    forward-reference summer_league_participation and break upgrade-from-base.
    op.execute(
        "ALTER TABLE summer_league_player_game_logs "
        "ADD COLUMN IF NOT EXISTS participation_id INTEGER"
    )
    op.create_index(
        "ix_summer_league_player_game_logs_participation_id",
        "summer_league_player_game_logs",
        ["participation_id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop participation_id column, both new tables, and both new enum types."""
    bind = op.get_bind()

    # 1. Drop the additive column + index (existing-table pattern).
    op.drop_index(
        "ix_summer_league_player_game_logs_participation_id",
        table_name="summer_league_player_game_logs",
        if_exists=True,
    )
    op.execute(
        "ALTER TABLE summer_league_player_game_logs "
        "DROP COLUMN IF EXISTS participation_id"
    )

    # 2. Drop summer_league_participation (FK → player_affiliations; must go first).
    op.drop_index(
        "ix_summer_league_participation_affiliation_id",
        table_name="summer_league_participation",
    )
    op.drop_index(
        "ix_summer_league_participation_source_player_id",
        table_name="summer_league_participation",
    )
    op.drop_index(
        "ix_summer_league_participation_competition_team",
        table_name="summer_league_participation",
    )
    op.drop_index(
        "ix_summer_league_participation_player_id",
        table_name="summer_league_participation",
    )
    op.drop_table("summer_league_participation")

    # 3. Drop player_affiliations.
    op.drop_index(
        "ix_player_affiliations_active",
        table_name="player_affiliations",
        postgresql_where=sa.text("superseded_at IS NULL AND retracted_at IS NULL"),
    )
    op.drop_index(
        "ix_player_affiliations_supersedes_id",
        table_name="player_affiliations",
    )
    op.drop_index("ix_player_affiliations_status", table_name="player_affiliations")
    op.drop_index(
        "ix_player_affiliations_player_id", table_name="player_affiliations"
    )
    op.drop_index(
        "ix_player_affiliations_nba_team_id", table_name="player_affiliations"
    )
    op.drop_table("player_affiliations")

    # 4. Drop the enum types (tables are gone; no FK references remain).
    postgresql.ENUM(name="affiliation_status_enum").drop(bind, checkfirst=True)
    postgresql.ENUM(name="player_affiliation_type_enum").drop(bind, checkfirst=True)

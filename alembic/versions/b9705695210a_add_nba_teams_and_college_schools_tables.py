"""add nba_teams and college_schools tables

Revision ID: b9705695210a
Revises: bc8962e9d4d3
Create Date: 2026-04-05
"""

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa

revision = "b9705695210a"
down_revision = "bc8962e9d4d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tables may already exist via AUTO_INIT_DB; create only if missing.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = inspector.get_table_names()

    if "nba_teams" not in existing:
        op.create_table(
            "nba_teams",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("abbreviation", sa.String, nullable=False),
            sa.Column("slug", sa.String, nullable=False),
            sa.Column("city", sa.String, nullable=True),
            sa.Column("conference", sa.String, nullable=True),
            sa.Column("division", sa.String, nullable=True),
            sa.Column("logo_url", sa.String, nullable=True),
            sa.Column("primary_color", sa.String, nullable=True),
            sa.Column("secondary_color", sa.String, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_nba_teams_name", "nba_teams", ["name"])
        op.create_index("ix_nba_teams_abbreviation", "nba_teams", ["abbreviation"], unique=True)
        op.create_index("ix_nba_teams_slug", "nba_teams", ["slug"], unique=True)

    if "college_schools" not in existing:
        op.create_table(
            "college_schools",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("slug", sa.String, nullable=False),
            sa.Column("conference", sa.String, nullable=True),
            sa.Column("logo_url", sa.String, nullable=True),
            sa.Column("primary_color", sa.String, nullable=True),
            sa.Column("secondary_color", sa.String, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_college_schools_name", "college_schools", ["name"], unique=True)
        op.create_index("ix_college_schools_slug", "college_schools", ["slug"], unique=True)
        op.create_index("ix_college_schools_conference", "college_schools", ["conference"])


def downgrade() -> None:
    op.drop_table("college_schools")
    op.drop_table("nba_teams")

"""Update player_similarity schema for dimensioned similarities

Revision ID: e4c5e5f4a2c3
Revises: c7b25de2a6f2
Create Date: 2025-11-23 13:30:00
"""

from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4c5e5f4a2c3"
down_revision = "c7b25de2a6f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add new enum + columns
    dimension_enum = sa.Enum(
        "anthro", "combine", "shooting", "composite", name="similarity_dimension_enum"
    )
    dimension_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "player_similarity",
        sa.Column("dimension", dimension_enum, nullable=True),
    )
    op.add_column(
        "player_similarity",
        sa.Column("distance", sa.Float(), nullable=True),
    )
    op.add_column(
        "player_similarity",
        sa.Column("overlap_pct", sa.Float(), nullable=True),
    )

    # 2) Backfill dimension from prior category values if present
    op.execute(
        """
        UPDATE player_similarity
        SET dimension = CASE category
            WHEN 'anthropometrics' THEN 'anthro'::similarity_dimension_enum
            WHEN 'combine_performance' THEN 'combine'::similarity_dimension_enum
            WHEN 'advanced_stats' THEN 'composite'::similarity_dimension_enum
            ELSE NULL
        END
        """
    )

    # 3) Drop old unique/indexes referencing category, then drop the column
    op.drop_constraint(
        "uq_player_similarity_anchor_comp_cat", "player_similarity", type_="unique"
    )
    op.drop_index(
        "ix_player_similarity_category_snapshot", table_name="player_similarity"
    )
    op.drop_column("player_similarity", "category")

    # 4) Enforce non-null dimension and new constraints/indexes
    op.alter_column(
        "player_similarity",
        "dimension",
        existing_type=dimension_enum,
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_player_similarity_anchor_comp_dim",
        "player_similarity",
        ["snapshot_id", "anchor_player_id", "comparison_player_id", "dimension"],
    )
    op.create_index(
        "ix_player_similarity_dimension_snapshot",
        "player_similarity",
        ["dimension", "snapshot_id"],
    )
    op.create_index(
        "ix_player_similarity_comparison_snapshot",
        "player_similarity",
        ["comparison_player_id", "snapshot_id"],
    )

    # 5) Drop the old enum type if it exists
    op.execute("DROP TYPE IF EXISTS similarity_category_enum")


def downgrade() -> None:
    # Recreate the old enum and column
    category_enum = sa.Enum(
        "anthropometrics",
        "combine_performance",
        "advanced_stats",
        name="similarity_category_enum",
    )
    category_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "player_similarity",
        sa.Column("category", category_enum, nullable=True),
    )

    # Backfill from dimension
    op.execute(
        """
        UPDATE player_similarity
        SET category = CASE dimension
            WHEN 'anthro' THEN 'anthropometrics'::similarity_category_enum
            WHEN 'combine' THEN 'combine_performance'::similarity_category_enum
            WHEN 'shooting' THEN 'combine_performance'::similarity_category_enum
            WHEN 'composite' THEN 'advanced_stats'::similarity_category_enum
            ELSE NULL
        END
        """
    )

    # Drop new constraints/indexes
    op.drop_constraint(
        "uq_player_similarity_anchor_comp_dim", "player_similarity", type_="unique"
    )
    op.drop_index(
        "ix_player_similarity_dimension_snapshot", table_name="player_similarity"
    )
    op.drop_index(
        "ix_player_similarity_comparison_snapshot", table_name="player_similarity"
    )

    # Reinstate prior constraint/index
    op.create_unique_constraint(
        "uq_player_similarity_anchor_comp_cat",
        "player_similarity",
        ["snapshot_id", "anchor_player_id", "comparison_player_id", "category"],
    )
    op.create_index(
        "ix_player_similarity_category_snapshot",
        "player_similarity",
        ["category", "snapshot_id"],
    )

    # Drop new columns
    op.drop_column("player_similarity", "dimension")
    op.drop_column("player_similarity", "distance")
    op.drop_column("player_similarity", "overlap_pct")

    op.execute("DROP TYPE IF EXISTS similarity_dimension_enum")

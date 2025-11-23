"""refactor combine shooting to one row per player-season

Revision ID: c7b25de2a6f2
Revises: 1f05df1c92b9
Create Date: 2025-11-23 12:00:00
"""

from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c7b25de2a6f2"
down_revision = "1f05df1c92b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "combine_shooting_results_new",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "player_id",
            sa.Integer(),
            sa.ForeignKey("players_master.id"),
            nullable=False,
        ),
        sa.Column(
            "season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False
        ),
        sa.Column(
            "position_id", sa.Integer(), sa.ForeignKey("positions.id"), nullable=True
        ),
        sa.Column("raw_position", sa.String(), nullable=True),
        sa.Column("off_dribble_fgm", sa.Integer(), nullable=True),
        sa.Column("off_dribble_fga", sa.Integer(), nullable=True),
        sa.Column("spot_up_fgm", sa.Integer(), nullable=True),
        sa.Column("spot_up_fga", sa.Integer(), nullable=True),
        sa.Column("three_point_star_fgm", sa.Integer(), nullable=True),
        sa.Column("three_point_star_fga", sa.Integer(), nullable=True),
        sa.Column("midrange_star_fgm", sa.Integer(), nullable=True),
        sa.Column("midrange_star_fga", sa.Integer(), nullable=True),
        sa.Column("three_point_side_fgm", sa.Integer(), nullable=True),
        sa.Column("three_point_side_fga", sa.Integer(), nullable=True),
        sa.Column("midrange_side_fgm", sa.Integer(), nullable=True),
        sa.Column("midrange_side_fga", sa.Integer(), nullable=True),
        sa.Column("free_throw_fgm", sa.Integer(), nullable=True),
        sa.Column("free_throw_fga", sa.Integer(), nullable=True),
        sa.Column("nba_stats_player_id", sa.Integer(), nullable=True),
        sa.Column("raw_player_name", sa.String(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("player_id", "season_id", name="uq_shooting_player_season"),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO combine_shooting_results_new (
                player_id,
                season_id,
                position_id,
                raw_position,
                off_dribble_fgm,
                off_dribble_fga,
                spot_up_fgm,
                spot_up_fga,
                three_point_star_fgm,
                three_point_star_fga,
                midrange_star_fgm,
                midrange_star_fga,
                three_point_side_fgm,
                three_point_side_fga,
                midrange_side_fgm,
                midrange_side_fga,
                free_throw_fgm,
                free_throw_fga,
                nba_stats_player_id,
                raw_player_name,
                ingested_at
            )
            SELECT
                player_id,
                season_id,
                max(position_id) AS position_id,
                max(raw_position) AS raw_position,
                max(CASE WHEN drill = 'off_dribble' THEN fgm END) AS off_dribble_fgm,
                max(CASE WHEN drill = 'off_dribble' THEN fga END) AS off_dribble_fga,
                max(CASE WHEN drill = 'spot_up' THEN fgm END) AS spot_up_fgm,
                max(CASE WHEN drill = 'spot_up' THEN fga END) AS spot_up_fga,
                max(CASE WHEN drill = 'three_point_star' THEN fgm END) AS three_point_star_fgm,
                max(CASE WHEN drill = 'three_point_star' THEN fga END) AS three_point_star_fga,
                max(CASE WHEN drill = 'midrange_star' THEN fgm END) AS midrange_star_fgm,
                max(CASE WHEN drill = 'midrange_star' THEN fga END) AS midrange_star_fga,
                max(CASE WHEN drill = 'three_point_side' THEN fgm END) AS three_point_side_fgm,
                max(CASE WHEN drill = 'three_point_side' THEN fga END) AS three_point_side_fga,
                max(CASE WHEN drill = 'midrange_side' THEN fgm END) AS midrange_side_fgm,
                max(CASE WHEN drill = 'midrange_side' THEN fga END) AS midrange_side_fga,
                max(CASE WHEN drill = 'free_throw' THEN fgm END) AS free_throw_fgm,
                max(CASE WHEN drill = 'free_throw' THEN fga END) AS free_throw_fga,
                max(nba_stats_player_id) AS nba_stats_player_id,
                max(raw_player_name) AS raw_player_name,
                coalesce(max(ingested_at), now()) AS ingested_at
            FROM combine_shooting_results
            GROUP BY player_id, season_id
            """
        )
    )

    op.drop_table("combine_shooting_results")
    op.rename_table("combine_shooting_results_new", "combine_shooting_results")
    op.create_index(
        op.f("ix_combine_shooting_results_player_id"),
        "combine_shooting_results",
        ["player_id"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_season_id"),
        "combine_shooting_results",
        ["season_id"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_position_id"),
        "combine_shooting_results",
        ["position_id"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_raw_position"),
        "combine_shooting_results",
        ["raw_position"],
    )


def downgrade() -> None:
    op.create_table(
        "combine_shooting_results_old",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "player_id",
            sa.Integer(),
            sa.ForeignKey("players_master.id"),
            nullable=False,
        ),
        sa.Column(
            "season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False
        ),
        sa.Column(
            "position_id", sa.Integer(), sa.ForeignKey("positions.id"), nullable=True
        ),
        sa.Column("raw_position", sa.String(), nullable=True),
        sa.Column("drill", sa.String(), nullable=False),
        sa.Column("fgm", sa.Integer(), nullable=True),
        sa.Column("fga", sa.Integer(), nullable=True),
        sa.Column("nba_stats_player_id", sa.Integer(), nullable=True),
        sa.Column("raw_player_name", sa.String(), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "player_id", "season_id", "drill", name="uq_shooting_player_season_drill"
        ),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO combine_shooting_results_old (
                player_id,
                season_id,
                position_id,
                raw_position,
                drill,
                fgm,
                fga,
                nba_stats_player_id,
                raw_player_name,
                ingested_at
            )
            SELECT
                csr.player_id,
                csr.season_id,
                csr.position_id,
                csr.raw_position,
                drills.drill,
                drills.fgm,
                drills.fga,
                csr.nba_stats_player_id,
                csr.raw_player_name,
                csr.ingested_at
            FROM combine_shooting_results csr
            CROSS JOIN LATERAL (
                VALUES
                    ('off_dribble', csr.off_dribble_fgm, csr.off_dribble_fga),
                    ('spot_up', csr.spot_up_fgm, csr.spot_up_fga),
                    ('three_point_star', csr.three_point_star_fgm, csr.three_point_star_fga),
                    ('midrange_star', csr.midrange_star_fgm, csr.midrange_star_fga),
                    ('three_point_side', csr.three_point_side_fgm, csr.three_point_side_fga),
                    ('midrange_side', csr.midrange_side_fgm, csr.midrange_side_fga),
                    ('free_throw', csr.free_throw_fgm, csr.free_throw_fga)
            ) AS drills(drill, fgm, fga)
            WHERE drills.fgm IS NOT NULL OR drills.fga IS NOT NULL
            """
        )
    )

    op.drop_table("combine_shooting_results")
    op.rename_table("combine_shooting_results_old", "combine_shooting_results")
    op.create_index(
        op.f("ix_combine_shooting_results_player_id"),
        "combine_shooting_results",
        ["player_id"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_season_id"),
        "combine_shooting_results",
        ["season_id"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_position_id"),
        "combine_shooting_results",
        ["position_id"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_raw_position"),
        "combine_shooting_results",
        ["raw_position"],
    )
    op.create_index(
        op.f("ix_combine_shooting_results_drill"),
        "combine_shooting_results",
        ["drill"],
    )

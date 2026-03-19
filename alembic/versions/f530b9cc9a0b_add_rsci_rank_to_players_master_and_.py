"""add rsci_rank to players_master and create player_college_stats table

Revision ID: f530b9cc9a0b
Revises: l1m2n3o4p5q6
Create Date: 2026-03-18 23:52:03.634379
"""
from alembic import op  # type: ignore[attr-defined]
import sqlalchemy as sa
from sqlalchemy import text

revision = 'f530b9cc9a0b'
down_revision = 'l1m2n3o4p5q6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add RSCI rank to players_master (idempotent: AUTO_INIT_DB may have added it)
    conn = op.get_bind()
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'players_master' AND column_name = 'rsci_rank'"
    ))
    if not result.fetchone():
        op.add_column(
            'players_master',
            sa.Column('rsci_rank', sa.Integer(), nullable=True),
        )

    # Create player_college_stats table (idempotent: AUTO_INIT_DB may have created it)
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'player_college_stats'"
    ))
    if result.fetchone():
        return

    op.create_table(
        'player_college_stats',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('player_id', sa.Integer(), sa.ForeignKey('players_master.id'), nullable=False),
        sa.Column('season', sa.String(), nullable=False),
        sa.Column('games', sa.Integer(), nullable=True),
        sa.Column('games_started', sa.Integer(), nullable=True),
        sa.Column('mpg', sa.Float(), nullable=True),
        sa.Column('ppg', sa.Float(), nullable=True),
        sa.Column('rpg', sa.Float(), nullable=True),
        sa.Column('apg', sa.Float(), nullable=True),
        sa.Column('spg', sa.Float(), nullable=True),
        sa.Column('bpg', sa.Float(), nullable=True),
        sa.Column('tov', sa.Float(), nullable=True),
        sa.Column('pf', sa.Float(), nullable=True),
        sa.Column('fg_pct', sa.Float(), nullable=True),
        sa.Column('three_p_pct', sa.Float(), nullable=True),
        sa.Column('three_pa', sa.Float(), nullable=True),
        sa.Column('ft_pct', sa.Float(), nullable=True),
        sa.Column('fta', sa.Float(), nullable=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('player_id', 'season', name='uq_college_stats_player_season'),
    )
    op.create_index('ix_player_college_stats_player_id', 'player_college_stats', ['player_id'])
    op.create_index('ix_player_college_stats_season', 'player_college_stats', ['season'])


def downgrade() -> None:
    op.drop_index('ix_player_college_stats_season', table_name='player_college_stats')
    op.drop_index('ix_player_college_stats_player_id', table_name='player_college_stats')
    op.drop_table('player_college_stats')
    op.drop_column('players_master', 'rsci_rank')

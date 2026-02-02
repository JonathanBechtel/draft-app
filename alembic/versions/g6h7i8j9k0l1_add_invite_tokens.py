"""Add invite tokens table and invited_at column.

This migration:
1. Creates the auth_invite_tokens table for user invitations
2. Adds invited_at column to auth_users table

Revision ID: g6h7i8j9k0l1
Revises: f5g6h7i8j9k0
Create Date: 2026-02-01
"""

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision = "g6h7i8j9k0l1"
down_revision = "f5g6h7i8j9k0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Add invited_at column to auth_users (if not exists)
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'auth_users' AND column_name = 'invited_at'"
        )
    )
    if result.fetchone() is None:
        op.add_column(
            "auth_users",
            sa.Column("invited_at", sa.DateTime(), nullable=True),
        )

    # Create auth_invite_tokens table (if not exists)
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'auth_invite_tokens'"
        )
    )
    if result.fetchone() is None:
        op.create_table(
            "auth_invite_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("auth_users.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "token_hash", sa.String(), nullable=False, unique=True, index=True
            ),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True, index=True),
        )


def downgrade() -> None:
    op.drop_table("auth_invite_tokens")
    op.drop_column("auth_users", "invited_at")

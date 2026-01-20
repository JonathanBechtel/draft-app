"""Add staff auth tables.

Revision ID: d8e1f2a3b4c5
Revises: c3d4e5f6a7b8
Create Date: 2026-01-19 00:00:00.000000
"""

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.auth import (
    AuthDatasetPermission,
    AuthEmailOutbox,
    AuthPasswordResetToken,
    AuthSession,
    AuthUser,
)

revision = "d8e1f2a3b4c5"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.create_all(
        bind=bind,
        tables=[
            AuthUser.__table__,  # type: ignore[attr-defined]
            AuthSession.__table__,  # type: ignore[attr-defined]
            AuthDatasetPermission.__table__,  # type: ignore[attr-defined]
            AuthEmailOutbox.__table__,  # type: ignore[attr-defined]
            AuthPasswordResetToken.__table__,  # type: ignore[attr-defined]
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.drop_all(
        bind=bind,
        tables=[
            AuthPasswordResetToken.__table__,  # type: ignore[attr-defined]
            AuthEmailOutbox.__table__,  # type: ignore[attr-defined]
            AuthDatasetPermission.__table__,  # type: ignore[attr-defined]
            AuthSession.__table__,  # type: ignore[attr-defined]
            AuthUser.__table__,  # type: ignore[attr-defined]
        ],
    )


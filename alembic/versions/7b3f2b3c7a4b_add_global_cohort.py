"""add global cohort"""

from alembic import op  # type: ignore[attr-defined]


# revision identifiers, used by Alembic.
revision = "7b3f2b3c7a4b"
down_revision = "4a25bc30bc2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE cohort_type_enum ADD VALUE IF NOT EXISTS 'global_scope'")


def downgrade() -> None:
    # Downgrading enums is non-trivial; no-op to avoid breaking existing rows.
    pass

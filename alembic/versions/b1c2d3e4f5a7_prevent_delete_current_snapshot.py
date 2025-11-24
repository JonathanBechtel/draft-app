"""feat: prevent deleting current metric snapshots via trigger

Revision ID: b1c2d3e4f5a7
Revises: 9ebe32d1120c
Create Date: 2025-11-17 00:15:00
"""

from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "b1c2d3e4f5a7"
down_revision = "9ebe32d1120c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_delete_current_snapshot()
        RETURNS trigger AS $$
        BEGIN
          IF OLD.is_current THEN
            RAISE EXCEPTION 'Cannot delete current snapshot (id=%). Demote first.', OLD.id;
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prevent_delete_current
        BEFORE DELETE ON metric_snapshots
        FOR EACH ROW EXECUTE FUNCTION prevent_delete_current_snapshot();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_prevent_delete_current ON metric_snapshots;
        DROP FUNCTION IF EXISTS prevent_delete_current_snapshot();
        """
    )

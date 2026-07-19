"""add environment_refresh summer league pipeline job

Revision ID: d3e4f5a6b7c8
Revises: a7b8c9d0e1f2
Create Date: 2026-07-19

Adds the ``ENVIRONMENT_REFRESH`` member to ``summer_league_pipeline_job_enum``
so the Competition Context incremental-refresh orchestration (#618) can record
its durable run outcome on the existing ``summer_league_pipeline_states`` table
alongside ``DESK``/``FULL_INGESTION``, reusing that table's generic
success/failure/timestamp columns rather than adding a parallel state table.

NOTE: SQLAlchemy's ``Enum`` type (no ``values_callable`` configured on
``SummerLeaguePipelineJob``'s column) stores the Python member **name**, not
its ``.value`` -- the existing DB labels are ``'DESK'`` / ``'FULL_INGESTION'``
(uppercase), not ``'desk'`` / ``'full_ingestion'``. The new label must match
that convention (``'ENVIRONMENT_REFRESH'``), confirmed by running this
migration against a disposable database and inspecting
``enum_range(NULL::summer_league_pipeline_job_enum)``.
"""

from alembic import op  # type: ignore[attr-defined]

revision = "d3e4f5a6b7c8"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the new enum value (Postgres requires this outside the tx block)."""
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE summer_league_pipeline_job_enum "
            "ADD VALUE IF NOT EXISTS 'ENVIRONMENT_REFRESH'"
        )


def downgrade() -> None:
    # Postgres does not support removing enum values; the value is harmless
    # if unused (mirrors bc9443ccd2b6_add_combine_score_enums.py).
    pass

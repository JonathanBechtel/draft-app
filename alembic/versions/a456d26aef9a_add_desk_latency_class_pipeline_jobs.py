"""add desk latency-class summer league pipeline jobs

Revision ID: a456d26aef9a
Revises: d3e4f5a6b7c8
Create Date: 2026-07-28

Adds ``DESK_FAST`` / ``DESK_PROJECTION`` / ``DESK_BACKBONE`` to
``summer_league_pipeline_job_enum`` so #699's latency-class partition
(`docs/plans/summer-league-desk-simplification-spec.md` §2) can record each
class's durable run outcome in its own ``summer_league_pipeline_states`` row.

Per-class rows, rather than one ``DESK`` row with a class column, are what let
"the tick was slow" resolve to *which* class, and what make the acceptance
metric measurable at all: the percentage of scheduled fast-class runs that
complete with advanced source data **inside live-game windows**. A single
shared row would average a 3-second poll and a 40-minute backbone pass into one
meaningless timestamp, and a failed backbone would clobber the fast class's
freshness -- the opposite of the "classes fail independently" requirement.

The pre-existing ``DESK`` label is deliberately left in place: the composite
``app.cli.sl_desk_tick`` entrypoint still runs and still reports under it, so
the partition can be deployed and rolled back without a flag day.

NOTE: SQLAlchemy's ``Enum`` type (no ``values_callable`` configured on
``SummerLeaguePipelineJob``'s column) stores the Python member **name**, not
its ``.value`` -- existing DB labels are ``'DESK'`` / ``'FULL_INGESTION'`` /
``'ENVIRONMENT_REFRESH'`` (uppercase). The new labels follow that convention,
matching ``d3e4f5a6b7c8``'s precedent.
"""

from alembic import op  # type: ignore[attr-defined]

revision = "a456d26aef9a"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

_NEW_LABELS = ("DESK_FAST", "DESK_PROJECTION", "DESK_BACKBONE")


def upgrade() -> None:
    """Add the new enum values (Postgres requires this outside the tx block).

    ``IF NOT EXISTS`` keeps this idempotent, which matters here beyond the
    usual re-run case: a from-scratch install may have created the type via
    ``SQLModel.metadata.create_all`` off the live enum class, in which case
    these labels already exist before this revision runs.
    """
    with op.get_context().autocommit_block():
        for label in _NEW_LABELS:
            op.execute(
                "ALTER TYPE summer_league_pipeline_job_enum "
                f"ADD VALUE IF NOT EXISTS '{label}'"
            )


def downgrade() -> None:
    # Postgres does not support removing enum values; unused labels are
    # harmless (mirrors d3e4f5a6b7c8 and bc9443ccd2b6).
    pass

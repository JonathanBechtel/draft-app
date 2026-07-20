"""Add distinct calculation version and exact raw-source provenance to Competition Context.

Revision ID: 16a524075c8e
Revises: n4o5p6q7r8s9
Create Date: 2026-07-20

Ticket #641 (Competition Context Explorer hardening): a published profile
carried a publication `version` mislabeled as "Calc version" in the UI, and no
distinct calculation-algorithm version existed separately from the metric
registry version. Provenance recorded per-source watermark/row-count but no
exact raw-run/source-record reference and never populated parse/source
status. These are additive columns on existing tables (not new tables), so
this migration uses targeted ``op.add_column``/``op.drop_column`` rather than
``SQLModel.metadata.create_all``/``drop_all`` per the repo migration
convention for existing-table changes.

* ``summer_league_environment_profiles.calculation_version`` — the
  aggregation-pipeline/calculation-logic version, distinct from
  ``registry_version`` (metric definitions) and ``version`` (a monotonic
  publication sequence number). Backfilled from the existing
  ``registry_version`` value for already-published rows (the best available
  approximation; the very next rebuild stamps the real calculation version).
* ``summer_league_environment_profiles.raw_run_ids`` — JSONB array of the
  exact ``summer_league_raw_runs.id`` values a profile can be traced back to.
* ``summer_league_environment_provenance.source_status`` — worst-case
  ``SummerLeagueRawRun.status`` across the contributing raw runs.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "16a524075c8e"
down_revision: Union[str, None] = "n4o5p6q7r8s9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add calculation_version/raw_run_ids/source_status columns."""
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("calculation_version", sa.String(), nullable=True),
    )
    # Backfill: the best available approximation for already-published rows
    # is the registry version they were built under; the next rebuild stamps
    # the real, distinct calculation version.
    op.execute(
        "UPDATE summer_league_environment_profiles "
        "SET calculation_version = registry_version "
        "WHERE calculation_version IS NULL"
    )
    op.alter_column(
        "summer_league_environment_profiles",
        "calculation_version",
        nullable=False,
    )
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("raw_run_ids", JSONB(), nullable=True),
    )
    op.add_column(
        "summer_league_environment_provenance",
        sa.Column("source_status", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Drop the added columns."""
    op.drop_column("summer_league_environment_provenance", "source_status")
    op.drop_column("summer_league_environment_profiles", "raw_run_ids")
    op.drop_column("summer_league_environment_profiles", "calculation_version")

"""Base classes to use as mixins elsewhere in app."""

from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class SoftDeleteMixin(SQLModel):
    deleted_at: Optional[datetime] = Field(default=None, index=True)


class DatedVersionMixin(SQLModel):
    """Columns that make a derived table a *dated, versioned* read model.

    Why this exists
    ---------------
    P2 (retain history by default) is a principle someone has to remember. The Summer
    League metrics rebuild violated it in part because nothing in the code made the rule
    visible: wiping a table and re-inserting looked like ordinary work. Inheriting this
    mixin makes the correct shape the default and the violation conspicuous in review —
    a table that carries these columns and is still deleted wholesale is obviously wrong.

    See ``docs/plans/north-star-architecture.md`` P2/P4 and the alignment doc §5b.

    Publish by version-flip, not by delete
    -------------------------------------
    The shape these columns are for: build the new version alongside the current one
    (nobody reads it — ``is_current`` still points at the old rows), then flip the pointer
    in one small transaction. Readers see the old coherent version until the instant they
    see the new coherent version, and no lock is held across the expensive work.

    Three version stamps, never conflated
    -------------------------------------
    Copied deliberately from ``summer_league_environment_profiles``, which already got
    this right, rather than from ``MetricSnapshot``:

    * ``version`` — publication sequence within a scope, bumped every rebuild whether or
      not anything changed. Not a content hash and not a logic version.
    * ``registry_version`` — the metric *definition* version (formulas, denominators,
      rounding, coverage rules).
    * ``calculation_version`` — the *pipeline* version. Bumped when how inputs are pooled
      or watermarked changes, even with identical metric definitions.

    Collapsing these is the failure they exist to prevent: without the split, "did this
    number change because the formula changed or because the data did?" is unanswerable.

    ``as_of`` is source currency, not process time
    ----------------------------------------------
    P4: a user-facing "as of" answers *how current is the information*, never *when did
    our job run*. ``as_of`` is therefore the max source-row timestamp the version was
    built from.

    Process time is deliberately **not** in this mixin. A table that wants it should add
    its own ``calculated_at``, so that rendering job-run time as "as of" requires a
    conscious choice rather than falling out of an inherited column sitting right next to
    the real one.

    What adopters still owe
    -----------------------
    This mixin cannot express the scope-dependent constraints, so each adopting table adds:

    * a unique constraint on ``(<scope columns>, version)``; and
    * a **partial** unique index on the scope columns ``WHERE is_current = true``, which
      is what actually enforces "exactly one current version per scope" in the database
      rather than in application code.

    ``summer_league_environment_profiles`` is the worked example of both.
    """

    version: int = Field(
        nullable=False,
        description=(
            "Publication sequence number within a scope, bumped every rebuild "
            "regardless of whether values changed. Distinct from registry_version "
            "and calculation_version."
        ),
    )
    is_current: bool = Field(
        default=False,
        nullable=False,
        description=(
            "Marks the single active version for this scope. Defaults to False so a "
            "version being built is invisible to readers until the pointer is flipped."
        ),
    )
    registry_version: str = Field(
        nullable=False,
        description=(
            "Metric-definition version this row was built under: formulas, "
            "denominators, rounding, coverage rules."
        ),
    )
    calculation_version: str = Field(
        nullable=False,
        description=(
            "Aggregation/calculation-pipeline version this row was built under. "
            "Distinct from registry_version: bumped when how inputs are pooled or "
            "watermarked changes even without a metric-definition change."
        ),
    )
    as_of: Optional[datetime] = Field(
        default=None,
        description=(
            "Source currency (P4): the max source-row timestamp this version was "
            "built from. Not the time the job ran -- that is operational telemetry "
            "and belongs in a separate column. Null when currency cannot be "
            "established, which callers must surface as degraded rather than render "
            "over."
        ),
    )

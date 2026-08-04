"""Unit tests for ``DatedVersionMixin``.

The mixin encodes P2 (retain history by default) and P4 (freshness means source
currency) as a type, so that longitudinal-first stops being a rule someone must
remember and becomes something a table inherits.

Summer League metric projections adopt it directly. These tests pin the properties
every adopter depends on, and the one property that keeps the mixin free — declaring it
creates no table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel

from app.schemas.base import DatedVersionMixin
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueDerivedAgg,
)


def test_mixin_declares_the_five_versioning_columns() -> None:
    """The stamps a dated read model needs, and no more."""
    assert set(DatedVersionMixin.model_fields) == {
        "version",
        "is_current",
        "registry_version",
        "calculation_version",
        "as_of",
    }


def test_defining_the_mixin_creates_no_table() -> None:
    """A mixin that quietly registered a table would be the opposite of free.

    This is what makes the mixin safe to land before anything adopts it: no metadata
    entry means no migration, no autogenerate diff, and nothing for a from-scratch
    bootstrap to create.
    """
    assert "datedversionmixin" not in SQLModel.metadata.tables
    assert not any(
        name.endswith("dated_version_mixin") for name in SQLModel.metadata.tables
    )


def test_is_current_defaults_to_false() -> None:
    """A version under construction must be invisible until the pointer is flipped.

    Defaulting to True would publish a half-built version the moment its first row was
    inserted — precisely the torn read the version-flip exists to prevent.
    """
    assert DatedVersionMixin.model_fields["is_current"].default is False
    assert SummerLeagueMetricContext.model_fields["is_current"].default is False
    assert SummerLeagueDerivedAgg.model_fields["is_current"].default is False


def test_as_of_is_optional_so_unknown_currency_is_representable() -> None:
    """P4 requires degrading visibly when currency cannot be established.

    A non-nullable ``as_of`` would force a fabricated timestamp -- most likely the job's
    own run time, which is exactly the process-time-as-freshness conflation P4 forbids.
    """
    field = DatedVersionMixin.model_fields["as_of"]
    assert field.default is None
    assert field.annotation == Optional[datetime]


def test_the_three_version_stamps_are_separate_fields() -> None:
    """Collapsing them makes "formula changed or data changed?" unanswerable."""
    stamps = {"version", "registry_version", "calculation_version"}
    assert stamps <= set(DatedVersionMixin.model_fields)
    # The publication counter is ordinal; the other two are opaque labels.
    assert DatedVersionMixin.model_fields["version"].annotation is int
    assert DatedVersionMixin.model_fields["registry_version"].annotation is str
    assert DatedVersionMixin.model_fields["calculation_version"].annotation is str


def test_process_time_is_not_bundled_into_the_mixin() -> None:
    """P4: an inherited ``calculated_at`` sitting beside ``as_of`` invites conflation.

    Tables that want process time add it themselves, so rendering job-run time as a
    user-facing "as of" is a conscious choice rather than a column that was simply there.
    """
    forbidden = {"calculated_at", "computed_at", "created_at", "updated_at", "run_at"}
    assert not (forbidden & set(DatedVersionMixin.model_fields))

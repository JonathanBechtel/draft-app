"""Unit coverage for #785's DatedVersionMixin adoption on the four versioned tables.

No DB required: these assert Python-level defaults/shapes only. The real-Postgres
sentinel-backfill and read-path-unchanged behavior is covered by
``tests/integration/test_add_version_stamps_to_legacy_snapshots_migration.py``.
"""

from __future__ import annotations

from app.models.fields import CohortType, MetricSource
from app.schemas.image_snapshots import (
    IMAGE_PIPELINE_CALCULATION_VERSION,
    LEGACY_VERSION_SENTINEL as IMAGE_SENTINEL,
    PlayerImageSnapshot,
)
from app.schemas.metrics import (
    LEGACY_VERSION_SENTINEL as METRIC_SENTINEL,
    METRIC_SNAPSHOT_VERSION_TAG,
    MetricSnapshot,
)
from app.schemas.summer_league_desk import SummerLeagueCohortBaseline
from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile


def test_metric_snapshot_is_current_defaults_to_false() -> None:
    """The mixin's is_current default ("invisible until flipped") lands as-is."""
    snapshot = MetricSnapshot(
        run_key="unit-run",
        cohort=CohortType.current_draft,
        source=MetricSource.combine_anthro,
        population_size=1,
    )
    assert snapshot.is_current is False


def test_player_image_snapshot_is_current_defaults_to_false() -> None:
    """Same default applies to PlayerImageSnapshot after mixin adoption."""
    snapshot = PlayerImageSnapshot(
        run_key="unit-run",
        style="default",
        cohort=CohortType.global_scope,
        image_size="1K",
        system_prompt="a prompt",
    )
    assert snapshot.is_current is False


def test_newly_published_metric_snapshot_carries_real_version_not_sentinel() -> None:
    """A publisher-constructed snapshot stamps the real tag, never the sentinel.

    Mirrors what ``app/cli/compute_metrics.py`` and
    ``app/cli/compute_combine_scores.py`` now pass at construction time.
    """
    snapshot = MetricSnapshot(
        run_key="unit-run",
        cohort=CohortType.current_draft,
        source=MetricSource.combine_anthro,
        population_size=10,
        version=1,
        is_current=False,
        registry_version=METRIC_SNAPSHOT_VERSION_TAG,
        calculation_version=METRIC_SNAPSHOT_VERSION_TAG,
    )
    assert snapshot.registry_version == METRIC_SNAPSHOT_VERSION_TAG
    assert snapshot.calculation_version == METRIC_SNAPSHOT_VERSION_TAG
    assert snapshot.registry_version != METRIC_SENTINEL
    assert snapshot.calculation_version != METRIC_SENTINEL


def test_newly_published_image_snapshot_carries_real_version_not_sentinel() -> None:
    """A publisher-constructed image snapshot stamps real values, not the sentinel.

    Mirrors what ``app/services/admin_image_service.py`` /
    ``app/services/player_enrichment_service.py`` /
    ``scripts/generate_player_images.py`` now pass at construction time.
    """
    snapshot = PlayerImageSnapshot(
        run_key="unit-run",
        version=1,
        is_current=False,
        style="default",
        cohort=CohortType.global_scope,
        image_size="1K",
        system_prompt="a prompt",
        system_prompt_version="v3",
        registry_version="v3",
        calculation_version=IMAGE_PIPELINE_CALCULATION_VERSION,
    )
    assert snapshot.registry_version == "v3"
    assert snapshot.calculation_version == IMAGE_PIPELINE_CALCULATION_VERSION
    assert snapshot.registry_version != IMAGE_SENTINEL
    assert snapshot.calculation_version != IMAGE_SENTINEL


def test_metric_snapshot_and_image_snapshot_share_dated_version_mixin_shape() -> None:
    """Both additive-migration tables carry all five mixin columns."""
    for table in (MetricSnapshot, PlayerImageSnapshot):
        columns = table.__table__.columns.keys()  # type: ignore[union-attr]
        for column in (
            "version",
            "is_current",
            "registry_version",
            "calculation_version",
            "as_of",
        ):
            assert column in columns, f"{table.__name__} is missing {column}"


def test_environment_profile_adopts_mixin_without_new_as_of_column() -> None:
    """Pure dedup: same four stamps inherited, no new `as_of` column added.

    Judgment call (#785): `source_watermark` already carries the P4 source-currency
    semantic under its pre-existing, actively-read name, so the mixin's `as_of` is
    excluded via ClassVar rather than duplicated as a second column.
    """
    columns = SummerLeagueEnvironmentProfile.__table__.columns.keys()  # type: ignore[attr-defined]
    for column in ("version", "is_current", "registry_version", "calculation_version"):
        assert column in columns
    assert "as_of" not in columns
    assert "source_watermark" in columns


def test_cohort_baseline_keeps_is_active_and_does_not_gain_mixin_columns() -> None:
    """Documented exception: is_active stays; no DatedVersionMixin columns land.

    Judgment call (#785): SummerLeagueCohortBaseline's shape only loosely matches
    the mixin (str `baseline_version` vs. int `version`; no registry/calculation
    version or as_of at all) and its live-read `is_active` flag predates
    `is_current`. Forcing partial adoption would be worse than the documented
    exception in the class docstring.
    """
    columns = SummerLeagueCohortBaseline.__table__.columns.keys()  # type: ignore[attr-defined]
    assert "is_active" in columns
    assert "is_current" not in columns
    for column in ("registry_version", "calculation_version", "as_of"):
        assert column not in columns

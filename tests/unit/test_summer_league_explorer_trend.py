"""Unit tests for the Competition Context trend-series builder (``_build_trend``).

Pure-Python coverage of ticket #642: a year with no current profile at all must
still produce an explicit chart point (never silently omitted), points must be
positionable by their real calendar-year offset rather than array index, and
the trend must carry its metric's display unit. No database is required —
``_CompetitionProfileView``/``MetricCoverageView`` are constructed directly.
"""

from __future__ import annotations

from app.schemas.summer_league_environment import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    SCOPE_KIND_COMPETITION,
    SCOPE_KIND_SEASON,
)
from app.services.summer_league_environment_registry import get_metric, unit_label
from app.services.summer_league_explorer_service import (
    MetricCoverageView,
    _build_trend,
    _CompetitionProfileView,
)

PACE = get_metric("pace_per_48")
THREE_FG_PCT = get_metric("three_fg_pct")
DISTINCT_TEAMS = get_metric("distinct_teams")


def _view(year: int, *, pace: float | None, coverage: str = COVERAGE_COMPLETE) -> _CompetitionProfileView:
    """Build a minimal season-scope profile view for one year."""
    return _CompetitionProfileView(
        profile_id=year,
        scope_key=f"season:{year}",
        scope_kind=SCOPE_KIND_SEASON,
        year=year,
        competition_id=None,
        venue_slug=None,
        display_name=f"{year} Summer League (all competitions)",
        version=1,
        registry_version="test",
        calculation_version="test",
        raw_run_ids=None,
        calculated_at=None,
        source_watermark=None,
        starts_on=None,
        ends_on=None,
        included_competitions=1,
        final_games=10,
        scheduled_games=0,
        distinct_teams=6,
        teams_represented=6,
        participation_count=None,
        player_games=60,
        appeared_players=60,
        appeared_unresolved=0,
        repeat_participants=None,
        raw_values={"pace_per_48": pace},
        coverage={
            "pace_per_48": MetricCoverageView(
                metric_key="pace_per_48",
                label=PACE.label,
                coverage=coverage,
                covered=10 if coverage == COVERAGE_COMPLETE else 0,
                eligible=10,
                reason=None if coverage == COVERAGE_COMPLETE else "box-partial",
            )
        },
        source_coverage={},
    )


def test_missing_year_gets_an_explicit_gap_point() -> None:
    """A calendar year with no profile at all still produces a point (never omitted)."""
    views = [_view(2023, pace=90.0), _view(2025, pace=95.0)]  # 2024 has no profile
    trend = _build_trend(
        "pace_per_48", PACE, views, scope_kind=SCOPE_KIND_SEASON, venue_slug=None
    )
    years = [p.year for p in trend.points]
    assert years == [2023, 2024, 2025]  # full domain, gap year included
    by_year = {p.year: p for p in trend.points}
    assert by_year[2024].value is None
    assert by_year[2024].has_profile is False
    assert by_year[2024].coverage == COVERAGE_UNAVAILABLE
    assert by_year[2023].has_profile is True
    assert by_year[2023].value == 90.0


def test_metric_null_inside_existing_profile_is_distinct_from_missing_year() -> None:
    """A null metric on an existing profile still reports ``has_profile=True``."""
    views = [
        _view(2023, pace=None, coverage=COVERAGE_PARTIAL),
        _view(2024, pace=95.0),
    ]
    trend = _build_trend(
        "pace_per_48", PACE, views, scope_kind=SCOPE_KIND_SEASON, venue_slug=None
    )
    by_year = {p.year: p for p in trend.points}
    assert by_year[2023].value is None
    assert by_year[2023].has_profile is True
    assert by_year[2023].coverage == COVERAGE_PARTIAL


def test_explicit_year_range_widens_domain_beyond_available_profiles() -> None:
    """``year_min``/``year_max`` extend the domain even where no profile exists."""
    views = [_view(2023, pace=90.0)]
    trend = _build_trend(
        "pace_per_48",
        PACE,
        views,
        scope_kind=SCOPE_KIND_SEASON,
        venue_slug=None,
        year_min=2021,
        year_max=2024,
    )
    years = [p.year for p in trend.points]
    assert years == [2021, 2022, 2023, 2024]
    by_year = {p.year: p for p in trend.points}
    assert by_year[2021].has_profile is False
    assert by_year[2022].has_profile is False
    assert by_year[2024].has_profile is False
    assert by_year[2023].value == 90.0


def test_no_views_and_no_range_yields_empty_points() -> None:
    """No surviving views and no explicit range produce an empty (not error) series."""
    trend = _build_trend(
        "pace_per_48", PACE, [], scope_kind=SCOPE_KIND_SEASON, venue_slug=None
    )
    assert trend.points == []


def test_trend_carries_the_metric_display_unit() -> None:
    """The trend series exposes the registry's short display unit (contract §6)."""
    views = [_view(2024, pace=95.0)]
    trend = _build_trend(
        "pace_per_48", PACE, views, scope_kind=SCOPE_KIND_SEASON, venue_slug=None
    )
    assert trend.unit == unit_label(PACE.unit) == "pace/48"

    ratio_views = [
        _CompetitionProfileView(
            profile_id=1,
            scope_key="season:2024",
            scope_kind=SCOPE_KIND_SEASON,
            year=2024,
            competition_id=None,
            venue_slug=None,
            display_name="2024 Summer League (all competitions)",
            version=1,
            registry_version="test",
            calculation_version="test",
            raw_run_ids=None,
            calculated_at=None,
            source_watermark=None,
            starts_on=None,
            ends_on=None,
            included_competitions=1,
            final_games=10,
            scheduled_games=0,
            distinct_teams=6,
            teams_represented=6,
            participation_count=None,
            player_games=60,
            appeared_players=60,
            appeared_unresolved=0,
            repeat_participants=None,
            raw_values={"three_fg_pct": 0.36},
            coverage={
                "three_fg_pct": MetricCoverageView(
                    metric_key="three_fg_pct",
                    label=THREE_FG_PCT.label,
                    coverage=COVERAGE_COMPLETE,
                    covered=10,
                    eligible=10,
                    reason=None,
                )
            },
            source_coverage={},
        )
    ]
    ratio_trend = _build_trend(
        "three_fg_pct",
        THREE_FG_PCT,
        ratio_views,
        scope_kind=SCOPE_KIND_SEASON,
        venue_slug=None,
    )
    assert ratio_trend.unit == "%"


def test_venue_series_keeps_scope_kind_and_venue_slug() -> None:
    """A competition-series trend preserves venue isolation metadata (contract §6)."""
    views = [_view(2023, pace=90.0), _view(2025, pace=95.0)]
    for v in views:
        v.scope_kind = SCOPE_KIND_COMPETITION
        v.venue_slug = "las_vegas"
    trend = _build_trend(
        "pace_per_48",
        PACE,
        views,
        scope_kind=SCOPE_KIND_COMPETITION,
        venue_slug="las_vegas",
    )
    assert trend.scope_kind == SCOPE_KIND_COMPETITION
    assert trend.venue_slug == "las_vegas"
    # 2024 has no profile in this venue series -> explicit gap, not omitted.
    by_year = {p.year: p for p in trend.points}
    assert by_year[2024].has_profile is False


def test_partial_coverage_stored_metric_renders_as_gap_not_raw_value() -> None:
    """A non-null stored column (e.g. distinct_teams) under partial coverage
    must still render as a gap, never as if it were a complete value (codex
    finding on PR #656: team-count trend showed a real number for a
    box-partial profile because the raw column itself is never null)."""
    view = _CompetitionProfileView(
        profile_id=2023,
        scope_key="season:2023",
        scope_kind=SCOPE_KIND_SEASON,
        year=2023,
        competition_id=None,
        venue_slug=None,
        display_name="2023 Summer League (all competitions)",
        version=1,
        registry_version="test",
        calculation_version="test",
        raw_run_ids=None,
        calculated_at=None,
        source_watermark=None,
        starts_on=None,
        ends_on=None,
        included_competitions=1,
        final_games=10,
        scheduled_games=0,
        distinct_teams=8,
        teams_represented=8,
        participation_count=None,
        player_games=60,
        appeared_players=60,
        appeared_unresolved=0,
        repeat_participants=None,
        raw_values={"distinct_teams": 8.0},
        coverage={
            "distinct_teams": MetricCoverageView(
                metric_key="distinct_teams",
                label=DISTINCT_TEAMS.label,
                coverage=COVERAGE_PARTIAL,
                covered=0,
                eligible=10,
                reason="box-partial",
            )
        },
        source_coverage={},
    )
    trend = _build_trend(
        "distinct_teams",
        DISTINCT_TEAMS,
        [view],
        scope_kind=SCOPE_KIND_SEASON,
        venue_slug=None,
    )
    assert len(trend.points) == 1
    assert trend.points[0].value is None
    assert trend.points[0].has_profile is True
    assert trend.points[0].coverage == COVERAGE_PARTIAL

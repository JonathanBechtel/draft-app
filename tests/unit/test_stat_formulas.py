"""Unit coverage for the source-agnostic shared stat formulas."""

from __future__ import annotations

from sqlalchemy import column

from app.services.stats.formulas import (
    net_rating,
    pace_per_48,
    pace_seconds_from_possessions,
    points_per_100,
    scale_python,
    vorp82,
    vorp_total,
    win_shares_per_40,
)
from app.services.stats.registry import (
    net_rating_expr,
    pace_per_48_expr,
    points_per_100_expr,
    scale_sql,
)


def test_scale_python_matches_the_published_display_factors() -> None:
    """Per-game, per-36, per-100, and totals retain their existing factors."""
    assert (
        scale_python(30.0, "per_game", gp=2, seconds=3000.0, pace_seconds=0.0) == 15.0
    )
    assert scale_python(30.0, "per_36", gp=2, seconds=3000.0, pace_seconds=0.0) == 21.6
    assert (
        scale_python(
            30.0, "per_100", gp=2, seconds=3000.0, pace_seconds=105.8333 * 60 * 48
        )
        == 30.0 * 100 / 105.8333
    )
    assert scale_python(30.0, "totals", gp=0, seconds=0.0, pace_seconds=0.0) == 30.0


def test_scale_python_returns_none_for_missing_denominators() -> None:
    """Undefined rate modes remain blank rather than fabricating zeroes."""
    assert scale_python(1.0, "per_game", gp=0, seconds=1.0, pace_seconds=1.0) is None
    assert scale_python(1.0, "per_36", gp=1, seconds=0.0, pace_seconds=1.0) is None
    assert scale_python(1.0, "per_100", gp=1, seconds=1.0, pace_seconds=0.0) is None
    assert pace_seconds_from_possessions(None) == 0.0


def test_remaining_formula_helpers_preserve_existing_numbers() -> None:
    """The residue helpers preserve the established rating and projection math."""
    assert points_per_100(30.0, 62.5) == 48.0
    assert pace_per_48(125.0, 300.0) == 100.0
    assert net_rating(110.0, 105.0) == 5.0
    assert net_rating(110.0, None) is None
    assert win_shares_per_40(1.5, 30.0) == 2.0
    assert win_shares_per_40(1.5, 0.0) is None
    assert vorp_total(0.0, 48.0) == 2.0 / 82.0
    assert vorp82(0.0, 0.25) == 0.5


def test_registry_sql_builders_use_the_same_neutral_field_shape() -> None:
    """SQL forms are parameterized by a box resolver rather than table imports."""
    assert scale_sql("PTS", "GP", "SEC", "PACE_SEC", "per_36") == (
        "PTS * 2160.0 / NULLIF(SEC, 0)"
    )
    assert "off_rating - def_rating" in str(net_rating_expr(column))
    assert "nullif" in str(pace_per_48_expr(column)).lower()
    assert "nullif" in str(points_per_100_expr(column)).lower()

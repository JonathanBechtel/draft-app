"""Unit tests for SL shot-chart share card (render model + SVG rendering)."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.services.share_cards.cache_keys import generate_filename, generate_title
from app.services.share_cards.render_models import (
    PlayerBadge,
    SLShotChartRenderModel,
    SLShotDiet,
    SLZoneRow,
)
from app.services.share_cards.svg_renderer import get_svg_renderer


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_model(*, with_diet: bool = True, suppressed: bool = False) -> SLShotChartRenderModel:
    """Build a minimal SLShotChartRenderModel for unit tests."""
    zones = [
        SLZoneRow(
            shot_zone_basic="Restricted Area",
            fga=30,
            fgm=18,
            fg_pct=0.600,
            freq_pct=0.345,
            pool_fg_pct=0.560,
            fg_pct_display="60.0%",
            freq_pct_display="35%",
            vs_pool="above",
        ),
        SLZoneRow(
            shot_zone_basic="In The Paint (Non-RA)",
            fga=10,
            fgm=4,
            fg_pct=0.400,
            freq_pct=0.115,
            pool_fg_pct=0.430,
            fg_pct_display="40.0%",
            freq_pct_display="12%",
            vs_pool="average",
        ),
        SLZoneRow(
            shot_zone_basic="Mid-Range",
            fga=5,
            fgm=1,
            fg_pct=0.200,
            freq_pct=0.057,
            pool_fg_pct=0.380,
            fg_pct_display="20.0%",
            freq_pct_display="6%",
            vs_pool="below",
        ),
        SLZoneRow(
            shot_zone_basic="Left Corner 3",
            fga=8,
            fgm=3,
            fg_pct=0.375,
            freq_pct=0.092,
            pool_fg_pct=None,
            fg_pct_display="37.5%",
            freq_pct_display="9%",
            vs_pool="unknown",
        ),
        SLZoneRow(
            shot_zone_basic="Right Corner 3",
            fga=12,
            fgm=5,
            fg_pct=0.417,
            freq_pct=0.138,
            pool_fg_pct=0.380,
            fg_pct_display="41.7%",
            freq_pct_display="14%",
            vs_pool="average",
        ),
        SLZoneRow(
            shot_zone_basic="Above the Break 3",
            fga=22,
            fgm=8,
            fg_pct=0.364,
            freq_pct=0.253,
            pool_fg_pct=0.340,
            fg_pct_display="36.4%",
            freq_pct_display="25%",
            vs_pool="average",
        ),
    ]
    diet: SLShotDiet | None = None
    if with_diet:
        diet = SLShotDiet(
            rim_rate=0.345,
            mid_rate=0.172,
            three_rate=0.483,
            corner3_rate=0.230,
            rim_display="35%",
            mid_display="17%",
            three_display="48%",
            corner3_display="23%",
        )
    return SLShotChartRenderModel(
        title="WEMBY — SL SHOT CHART",
        subtitle="Summer League · Career · 87 FGA",
        player=PlayerBadge(name="Victor Wembanyama", subtitle="C | 2023", has_photo=False),
        total_fga=87,
        suppressed=suppressed,
        zones=zones,
        shot_diet=diet,
        accent_color="#f97316",
    )


# ---------------------------------------------------------------------------
# Model dataclass tests
# ---------------------------------------------------------------------------


class TestSLShotChartRenderModel:
    """Tests for SLShotChartRenderModel dataclass construction."""

    def test_model_builds_with_all_zones(self):
        """Should build a model with 6 zone rows and correct total_fga."""
        model = _make_model()

        assert model.total_fga == 87
        assert len(model.zones) == 6
        assert model.suppressed is False

    def test_model_suppressed_flag(self):
        """suppressed=True propagates to the render model correctly."""
        model = _make_model(suppressed=True)

        assert model.suppressed is True

    def test_model_without_diet(self):
        """Model with no shot_diet is valid and shot_diet is None."""
        model = _make_model(with_diet=False)

        assert model.shot_diet is None

    def test_zone_vs_pool_values(self):
        """Each zone's vs_pool field should be one of the four valid labels."""
        model = _make_model()
        valid = {"above", "below", "average", "unknown"}
        for zone in model.zones:
            assert zone.vs_pool in valid, f"Unexpected vs_pool: {zone.vs_pool!r}"

    def test_asdict_is_serialisable(self):
        """dataclasses.asdict should produce a plain dict (no custom objects)."""
        model = _make_model()
        d = asdict(model)
        assert isinstance(d, dict)
        assert "zones" in d
        assert isinstance(d["zones"], list)


# ---------------------------------------------------------------------------
# Cache key / filename / title generation
# ---------------------------------------------------------------------------


class TestSLShotChartCacheKeys:
    """Tests for cache-key, filename, and title generation for sl_shot_chart."""

    def test_filename_contains_player_slug_and_suffix(self):
        """Filename should include player name slug and sl-shot-chart suffix."""
        filename = generate_filename("sl_shot_chart", ["Victor Wembanyama"])

        assert "victor-wembanyama" in filename
        assert "sl-shot-chart" in filename
        assert filename.endswith(".png")

    def test_title_contains_player_name(self):
        """Title should identify the player and card type."""
        title = generate_title("sl_shot_chart", ["Victor Wembanyama"])

        assert "Victor Wembanyama" in title
        assert "SL Shot Chart" in title


# ---------------------------------------------------------------------------
# SVG rendering smoke test
# ---------------------------------------------------------------------------


class TestSLShotChartSVGRender:
    """Smoke tests: verify the SVG template renders without errors."""

    def test_render_produces_valid_svg(self):
        """SVG renderer should return a non-empty string containing <svg> tag."""
        model = _make_model()
        renderer = get_svg_renderer()
        context = asdict(model)
        # Jinja doesn't expose @property rendered on ContextLine; sl_shot_chart
        # has no context_line so no enrichment needed here.

        svg = renderer.render("sl_shot_chart.svg", context)

        assert isinstance(svg, str)
        assert "<svg" in svg
        assert "nbadraft.app" in svg  # footer watermark

    def test_render_includes_player_name(self):
        """Rendered SVG should contain the player's name text."""
        model = _make_model()
        renderer = get_svg_renderer()
        context = asdict(model)

        svg = renderer.render("sl_shot_chart.svg", context)

        assert "Victor Wembanyama" in svg

    def test_render_includes_zone_names(self):
        """Each zone name should appear in the rendered SVG."""
        model = _make_model()
        renderer = get_svg_renderer()
        context = asdict(model)

        svg = renderer.render("sl_shot_chart.svg", context)

        assert "Restricted Area" in svg
        assert "Above the Break 3" in svg

    def test_render_includes_shot_diet(self):
        """Shot diet values should appear in the rendered SVG when present."""
        model = _make_model(with_diet=True)
        renderer = get_svg_renderer()
        context = asdict(model)

        svg = renderer.render("sl_shot_chart.svg", context)

        assert "35%" in svg  # rim_display
        assert "48%" in svg  # three_display

    def test_render_suppressed_shows_note(self):
        """Suppressed=True should include the small-sample warning text."""
        model = _make_model(suppressed=True)
        renderer = get_svg_renderer()
        context = asdict(model)

        svg = renderer.render("sl_shot_chart.svg", context)

        assert "Small sample" in svg

    def test_render_without_diet_omits_diet_section(self):
        """When shot_diet is None, the SVG should not contain diet section headers."""
        model = _make_model(with_diet=False)
        renderer = get_svg_renderer()
        context = asdict(model)

        svg = renderer.render("sl_shot_chart.svg", context)

        assert "Shot Diet" not in svg

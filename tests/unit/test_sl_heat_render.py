"""Unit tests for the share-card SL heat renderer."""

from app.services.share_cards.sl_heat_render import (
    _classify_zone,
    _Dot,
    render_shot_heat_data_uri,
)


def test_empty_dots_returns_none():
    """No shots → no image."""
    assert render_shot_heat_data_uri([], {}, has_pool=False) is None


def test_backcourt_only_returns_none():
    """Heaves beyond the half court are dropped; nothing left to render."""
    dots = [_Dot(0.0, 800.0, True), _Dot(0.0, 900.0, False)]
    assert render_shot_heat_data_uri(dots, {}, has_pool=False) is None


def test_renders_png_data_uri():
    """A handful of real shots produces a base64 PNG data URI."""
    dots = [
        _Dot(0.0, 10.0, True),
        _Dot(-230.0, 20.0, True),
        _Dot(50.0, 220.0, False),
        _Dot(10.0, 150.0, True),
    ]
    uri = render_shot_heat_data_uri(dots, {}, has_pool=False)
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    assert len(uri) > 100  # non-trivial payload


def test_pool_path_renders():
    """The diverging (vs-pool) path renders when a pool baseline is supplied."""
    dots = [_Dot(0.0, 10.0, True), _Dot(-230.0, 20.0, False)]
    zone_pool = {"Restricted Area": 0.6, "Left Corner 3": 0.38}
    uri = render_shot_heat_data_uri(dots, zone_pool, has_pool=True)
    assert uri is not None and uri.startswith("data:image/png;base64,")


def test_classify_zone():
    """Coordinate → NBA zone classification matches the on-page component."""
    assert _classify_zone(0, 10) == "Restricted Area"
    assert _classify_zone(-230, 20) == "Left Corner 3"
    assert _classify_zone(230, 20) == "Right Corner 3"
    assert _classify_zone(0, 260) == "Above the Break 3"
    assert _classify_zone(40, 120) == "In The Paint (Non-RA)"
    assert _classify_zone(120, 120) == "Mid-Range"

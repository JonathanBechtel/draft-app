"""Unit coverage for cumulative trend SVG share-card rendering."""

from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.share_cards.cache_keys import generate_filename, generate_title
from app.services.share_cards.export_service import ImageExportService
from app.services.share_cards.render_models import (
    PlayerBadge,
    TrendChartLine,
    TrendChartPoint,
    TrendRenderModel,
)
from app.services.share_cards.svg_renderer import get_svg_renderer


def _model() -> TrendRenderModel:
    """Return a deterministic three-line model with cohort bands."""
    return TrendRenderModel(
        title="TEST — TREND",
        subtitle="competition:42 · cumulative through day",
        player=PlayerBadge(name="Test Player", subtitle="G | School", has_photo=False),
        lines=[
            TrendChartLine(
                key="gmsc",
                label="GmSc",
                color="#2563eb",
                points=[
                    TrendChartPoint("2024-07-01", "7.0", 170, 580, 560, 600),
                    TrendChartPoint("2024-07-03", "8.0", 2270, 530, 510, 550),
                ],
            ),
            TrendChartLine(
                key="ts_pct",
                label="TS%",
                color="#d97706",
                points=[TrendChartPoint("2024-07-01", "55.0%", 170, 805, 785, 825)],
            ),
            TrendChartLine(
                key="bpm",
                label="BPM",
                color="#e11d48",
                points=[TrendChartPoint("2024-07-01", "+1.2", 170, 1030, 1010, 1050)],
            ),
        ],
        as_of="2026-07-20",
    )


def test_trend_share_card_svg_contains_lines_band_and_freshness() -> None:
    """The SVG keeps the three lines, cohort band, and source-currency label."""
    svg = get_svg_renderer().render("sl_trend.svg", asdict(_model()))

    assert svg.startswith("<?xml")
    assert 'viewBox="0 0 2400 1260"' in svg
    assert "GmSc" in svg and "TS%" in svg and "BPM" in svg
    assert 'fill-opacity=".15"' in svg
    assert "Source as of 2026-07-20" in svg
    assert "Test Player" in svg


def test_trend_share_card_cache_metadata() -> None:
    """Trend exports use stable, recognizable download metadata."""
    assert generate_filename("sl_trend", ["Test Player"]).endswith("-trend.png")
    assert generate_title("sl_trend", ["Test Player"]) == "Test Player — Trend"


@pytest.mark.asyncio
async def test_trend_export_versions_url_by_rendered_daily_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed daily-close model gets a distinct content-addressed public URL."""
    service = ImageExportService.__new__(ImageExportService)
    service.db = AsyncMock()
    service.storage = MagicMock()
    service.storage.check_cache.return_value = None
    service.storage.upload.side_effect = (
        lambda cache_key, *_args, **_kwargs: f"https://example.test/{cache_key}"
    )
    service.renderer = MagicMock()
    service.rasterizer = MagicMock()
    service.rasterizer.rasterize.return_value = b"png"
    first_model = _model()
    second_model = _model()
    second_model.as_of = "2026-07-21"
    build_model = AsyncMock(side_effect=[first_model, second_model])
    monkeypatch.setattr(service, "_build_model", build_model)

    first = await service.export(
        "sl_trend",
        [7],
        {"scope_key": "competition:42", "metric_keys": ["gmsc", "ts_pct", "bpm"]},
    )
    second = await service.export(
        "sl_trend",
        [7],
        {"scope_key": "competition:42", "metric_keys": ["gmsc", "ts_pct", "bpm"]},
    )

    assert service.storage.check_cache.call_count == 2
    assert build_model.await_count == 2
    first_key = service.storage.upload.call_args_list[0].args[0]
    second_key = service.storage.upload.call_args_list[1].args[0]
    assert first_key != second_key
    assert first["url"] != second["url"]
    assert first["cached"] is False and second["cached"] is False

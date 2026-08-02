"""Unit coverage for cumulative trend SVG share-card rendering."""

from dataclasses import asdict
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.services.share_cards import model_builders
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
async def test_trend_builder_compacts_lanes_when_middle_metric_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BPM occupies lane one when TS% has no published point."""
    day = date(2026, 7, 20)
    points = [
        TrendPoint(
            metric_key="gmsc",
            effective_day=day,
            value=8.0,
            cohort_band=TrendCohortBand(median=8.0, q1=7.0, q3=9.0),
        ),
        TrendPoint(
            metric_key="bpm",
            effective_day=day,
            value=4.0,
            cohort_band=TrendCohortBand(median=4.0, q1=3.0, q3=5.0),
        ),
    ]
    monkeypatch.setattr(
        model_builders, "get_daily_trend", AsyncMock(return_value=points)
    )
    monkeypatch.setattr(
        model_builders,
        "_resolve_player_info",
        AsyncMock(return_value=("Test Player", "test-player", "G", None, [], 2026)),
    )
    monkeypatch.setattr(
        model_builders,
        "_build_player_badge",
        AsyncMock(return_value=PlayerBadge(name="Test Player", subtitle="G")),
    )

    model = await model_builders.build_sl_trend_model(
        AsyncMock(),
        [7],
        {"scope_key": "competition:42", "metric_keys": ["gmsc", "ts_pct", "bpm"]},
    )

    assert [line.key for line in model.lines] == ["gmsc", "bpm"]
    assert model.lines[1].points[0].y == pytest.approx(730.0)


@pytest.mark.asyncio
async def test_trend_builder_deduplicates_metric_keys_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated requested keys produce one query key and one rendered lane."""
    day = date(2026, 7, 20)
    load_trend = AsyncMock(
        return_value=[
            TrendPoint(
                metric_key="gmsc",
                effective_day=day,
                value=8.0,
                cohort_band=TrendCohortBand(median=8.0, q1=7.0, q3=9.0),
            )
        ]
    )
    monkeypatch.setattr(model_builders, "get_daily_trend", load_trend)
    monkeypatch.setattr(
        model_builders,
        "_resolve_player_info",
        AsyncMock(return_value=("Test Player", "test-player", "G", None, [], 2026)),
    )
    monkeypatch.setattr(
        model_builders,
        "_build_player_badge",
        AsyncMock(return_value=PlayerBadge(name="Test Player", subtitle="G")),
    )

    model = await model_builders.build_sl_trend_model(
        AsyncMock(),
        [7],
        {"scope_key": "competition:42", "metric_keys": ["gmsc"] * 3},
    )

    assert load_trend.await_args is not None
    assert load_trend.await_args.kwargs["metric_keys"] == ("gmsc",)
    assert [line.key for line in model.lines] == ["gmsc"]


@pytest.mark.asyncio
async def test_trend_builder_rejects_more_than_three_unique_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixed three-lane card rejects excess work before querying trends."""
    load_trend = AsyncMock()
    monkeypatch.setattr(model_builders, "get_daily_trend", load_trend)

    with pytest.raises(ValueError, match="at most 3 unique"):
        await model_builders.build_sl_trend_model(
            AsyncMock(),
            [7],
            {
                "scope_key": "competition:42",
                "metric_keys": ["gmsc", "ts_pct", "bpm", "minutes"],
            },
        )

    load_trend.assert_not_awaited()


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

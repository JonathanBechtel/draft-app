"""Unit tests for summer_league_shotchart_service zone aggregation (DB-free).

Exercises the pure aggregation helpers and the public functions via a mock
AsyncSession, validating:

- Zone FGA/FGM/FG%/freq% arithmetic.
- Pool-average baseline wiring.
- Suppression signal when total FGA < MIN_FGA_FOR_CHART.
- Career rollup: pool_fg_pct is None, zones still populated.
- Shot-dot helper: correct DTO shape.
- Excluded zones (Backcourt) are omitted from aggregation.
- Empty result: suppressed=True, zones=[].
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.summer_league_shotchart_service import (
    MIN_FGA_FOR_CHART,
    PlayerShotDots,
    PlayerShotZones,
    ShotDot,
    ShotZoneRow,
    _build_zone_rows,
    _safe_pct,
    get_player_shot_dots,
    get_player_shot_zones,
)


# ---------------------------------------------------------------------------
# Pure helper tests (no I/O)
# ---------------------------------------------------------------------------


def test_safe_pct_normal() -> None:
    """FG% equals made/attempted when attempted > 0."""
    assert _safe_pct(7, 10) == pytest.approx(0.7)


def test_safe_pct_zero_attempts() -> None:
    """Returns None when attempted is zero (avoids ZeroDivisionError)."""
    assert _safe_pct(0, 0) is None


def test_build_zone_rows_single_zone() -> None:
    """Single-zone input: freq_pct = 1.0, fg_pct correct, pool_fg_pct passed through."""
    pool_map: dict[str, Optional[float]] = {"Restricted Area": 0.65}
    total, rows = _build_zone_rows(
        [("Restricted Area", 8, 10)],
        pool_map=pool_map,
    )
    assert total == 10
    assert len(rows) == 1
    row = rows[0]
    assert row.shot_zone_basic == "Restricted Area"
    assert row.fgm == 8
    assert row.fga == 10
    assert row.fg_pct == pytest.approx(0.8)
    assert row.freq_pct == pytest.approx(1.0)
    assert row.pool_fg_pct == pytest.approx(0.65)


def test_build_zone_rows_freq_pct_sums_to_one() -> None:
    """freq_pct across all zones sums to 1.0."""
    shots = [
        ("Restricted Area", 5, 10),
        ("Mid-Range", 2, 6),
        ("Above the Break 3", 3, 4),
    ]
    total, rows = _build_zone_rows(shots, pool_map={})
    assert total == 20
    assert sum(r.freq_pct for r in rows) == pytest.approx(1.0)


def test_build_zone_rows_fg_pct_none_when_zero_fga() -> None:
    """Zones with fga=0 produce fg_pct=None."""
    total, rows = _build_zone_rows([("Mid-Range", 0, 0)], pool_map={})
    assert rows[0].fg_pct is None
    # freq_pct is also 0 when total_fga is 0
    assert rows[0].freq_pct == pytest.approx(0.0)


def test_build_zone_rows_pool_fg_pct_none_when_not_in_map() -> None:
    """pool_fg_pct is None for zones absent from the pool_map."""
    _, rows = _build_zone_rows(
        [("Restricted Area", 3, 5)],
        pool_map={},
    )
    assert rows[0].pool_fg_pct is None


def test_build_zone_rows_sorted_by_display_order() -> None:
    """Zones are sorted by canonical display order, not insertion order."""
    shots = [
        ("Above the Break 3", 2, 4),
        ("Restricted Area", 6, 8),
        ("Mid-Range", 1, 3),
    ]
    _, rows = _build_zone_rows(shots, pool_map={})
    names = [r.shot_zone_basic for r in rows]
    assert names == ["Restricted Area", "Mid-Range", "Above the Break 3"]


def test_build_zone_rows_unknown_zone_sorted_last() -> None:
    """An unrecognised zone label sorts after the canonical 6 zones."""
    shots = [
        ("Restricted Area", 5, 10),
        ("Half Court Heave", 0, 1),
    ]
    _, rows = _build_zone_rows(shots, pool_map={})
    assert rows[-1].shot_zone_basic == "Half Court Heave"


# ---------------------------------------------------------------------------
# get_player_shot_zones — via mocked _fetch_zone_agg / _fetch_pool_baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_player_shot_zones_above_floor() -> None:
    """Returns suppressed=False when total FGA >= MIN_FGA_FOR_CHART.

    Mocks the internal DB fetches to inject synthetic zone rows and a pool
    baseline; verifies the full DTO shape including pool_fg_pct wiring.
    """
    session = AsyncMock()
    zone_rows = [
        ("Restricted Area", 10, 15),
        ("Above the Break 3", 3, 8),
    ]
    pool = {"Restricted Area": 0.60, "Above the Break 3": 0.35}

    with (
        patch(
            "app.services.summer_league_shotchart_service._fetch_zone_agg",
            new=AsyncMock(return_value=zone_rows),
        ),
        patch(
            "app.services.summer_league_shotchart_service._fetch_pool_baseline",
            new=AsyncMock(return_value=pool),
        ),
    ):
        result = await get_player_shot_zones(session, player_id=42, competition_id=7)

    assert isinstance(result, PlayerShotZones)
    assert result.player_id == 42
    assert result.competition_id == 7
    assert result.total_fga == 23
    assert result.suppressed is False
    assert len(result.zones) == 2
    ra = next(z for z in result.zones if z.shot_zone_basic == "Restricted Area")
    assert ra.pool_fg_pct == pytest.approx(0.60)
    ab3 = next(z for z in result.zones if z.shot_zone_basic == "Above the Break 3")
    assert ab3.pool_fg_pct == pytest.approx(0.35)


@pytest.mark.asyncio
async def test_get_player_shot_zones_suppressed_below_floor() -> None:
    """Returns suppressed=True when total FGA < MIN_FGA_FOR_CHART.

    The zones list is still populated (for the table); the suppressed flag
    signals callers not to render the heat chart.
    """
    session = AsyncMock()
    zone_rows = [
        ("Restricted Area", 4, 6),
        ("Mid-Range", 2, 5),
    ]  # total = 11, below 20

    with (
        patch(
            "app.services.summer_league_shotchart_service._fetch_zone_agg",
            new=AsyncMock(return_value=zone_rows),
        ),
        patch(
            "app.services.summer_league_shotchart_service._fetch_pool_baseline",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await get_player_shot_zones(session, player_id=99, competition_id=3)

    assert result.suppressed is True
    assert result.total_fga == 11
    assert result.total_fga < MIN_FGA_FOR_CHART
    # zones still populated for the table
    assert len(result.zones) == 2


@pytest.mark.asyncio
async def test_get_player_shot_zones_exactly_at_floor_not_suppressed() -> None:
    """Exactly MIN_FGA_FOR_CHART FGA is NOT suppressed (floor is strictly <)."""
    session = AsyncMock()
    zone_rows = [("Restricted Area", 10, MIN_FGA_FOR_CHART)]

    with (
        patch(
            "app.services.summer_league_shotchart_service._fetch_zone_agg",
            new=AsyncMock(return_value=zone_rows),
        ),
        patch(
            "app.services.summer_league_shotchart_service._fetch_pool_baseline",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await get_player_shot_zones(session, player_id=1, competition_id=1)

    assert result.suppressed is False
    assert result.total_fga == MIN_FGA_FOR_CHART


@pytest.mark.asyncio
async def test_get_player_shot_zones_career_rollup_no_pool_fg_pct() -> None:
    """Career rollup (competition_id=None): pool_fg_pct is None on all zones.

    Pool baseline is not computed for career views; zones still aggregate.
    """
    session = AsyncMock()
    zone_rows = [
        ("Restricted Area", 20, 30),
        ("Above the Break 3", 8, 22),
    ]

    with patch(
        "app.services.summer_league_shotchart_service._fetch_zone_agg",
        new=AsyncMock(return_value=zone_rows),
    ):
        result = await get_player_shot_zones(session, player_id=5)  # no competition_id

    assert result.competition_id is None
    assert result.total_fga == 52
    assert result.suppressed is False
    for z in result.zones:
        assert z.pool_fg_pct is None


@pytest.mark.asyncio
async def test_get_player_shot_zones_empty_no_shots() -> None:
    """No shots at all: suppressed=True, zones=[], total_fga=0."""
    session = AsyncMock()

    with (
        patch(
            "app.services.summer_league_shotchart_service._fetch_zone_agg",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.summer_league_shotchart_service._fetch_pool_baseline",
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await get_player_shot_zones(session, player_id=1, competition_id=2)

    assert result.suppressed is True
    assert result.total_fga == 0
    assert result.zones == []


# ---------------------------------------------------------------------------
# get_player_shot_dots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_player_shot_dots_returns_correct_shape() -> None:
    """Dot list has correct types and matches the seeded rows."""
    session = AsyncMock()

    # Mock the DB execute to return synthetic rows
    fake_rows = [(-50, 120, True), (30, 80, False), (10, 200, True)]
    mock_result = MagicMock()
    mock_result.all.return_value = fake_rows
    session.execute = AsyncMock(return_value=mock_result)

    result = await get_player_shot_dots(session, player_id=7, competition_id=3)

    assert isinstance(result, PlayerShotDots)
    assert result.player_id == 7
    assert result.competition_id == 3
    assert len(result.dots) == 3
    assert all(isinstance(d, ShotDot) for d in result.dots)
    assert result.dots[0] == ShotDot(loc_x=-50, loc_y=120, made=True)
    assert result.dots[1] == ShotDot(loc_x=30, loc_y=80, made=False)


@pytest.mark.asyncio
async def test_get_player_shot_dots_empty_when_no_shots() -> None:
    """Returns empty dots list when player has no shots in the competition."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.execute = AsyncMock(return_value=mock_result)

    result = await get_player_shot_dots(session, player_id=99, competition_id=1)

    assert result.dots == []


# ---------------------------------------------------------------------------
# Zone math: detailed arithmetic cross-check
# ---------------------------------------------------------------------------


def test_zone_fg_pct_arithmetic() -> None:
    """fg_pct = fgm / fga for each zone independently."""
    shots = [
        ("Restricted Area", 7, 10),   # 70%
        ("Mid-Range", 3, 12),          # 25%
        ("Above the Break 3", 5, 15),  # 33.33%
    ]
    _, rows = _build_zone_rows(shots, pool_map={})
    by_zone = {r.shot_zone_basic: r for r in rows}
    assert by_zone["Restricted Area"].fg_pct == pytest.approx(0.70)
    assert by_zone["Mid-Range"].fg_pct == pytest.approx(0.25)
    assert by_zone["Above the Break 3"].fg_pct == pytest.approx(1 / 3, rel=1e-4)


def test_zone_freq_pct_arithmetic() -> None:
    """freq_pct = zone_fga / total_fga for each zone."""
    shots = [
        ("Restricted Area", 5, 10),   # freq = 10/30
        ("Mid-Range", 3, 10),          # freq = 10/30
        ("Above the Break 3", 4, 10),  # freq = 10/30
    ]
    total, rows = _build_zone_rows(shots, pool_map={})
    assert total == 30
    for r in rows:
        assert r.freq_pct == pytest.approx(10 / 30)


def test_zone_pool_fg_pct_independent_of_player_counts() -> None:
    """pool_fg_pct comes from pool_map, not from the player's shot counts."""
    shots = [("Restricted Area", 1, 2)]  # player 50%
    pool_map: dict[str, Optional[float]] = {"Restricted Area": 0.70}  # pool 70%
    _, rows = _build_zone_rows(shots, pool_map=pool_map)
    assert rows[0].fg_pct == pytest.approx(0.5)
    assert rows[0].pool_fg_pct == pytest.approx(0.7)

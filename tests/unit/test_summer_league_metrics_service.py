"""Unit tests for the Summer League advanced-metrics read service helpers.

Cover the grain rules in isolation (no DB): additive shares (WS, VORP) are
summed across competitions, PER/BPM are minute-weighted averages, WS/40 is
recomputed from the summed shares, and ``None`` inputs never drag an average
toward zero.
"""

from __future__ import annotations

from typing import Optional

from app.services.summer_league_metrics_service import (
    PlayerMetricSeason,
    _career,
    _resolve_competition,
    _weighted_mean,
)

_COMPS = [(2025, "las_vegas"), (2025, "salt_lake_city"), (2024, "las_vegas")]


def _season(
    *,
    minutes: float,
    per: Optional[float] = None,
    ts: Optional[float] = None,
    pts: int = 0,
    fga: int = 0,
    fta: int = 0,
    tov: int = 0,
    bpm: Optional[float] = None,
    ws: Optional[float] = None,
    vorp: Optional[float] = None,
    ws82: Optional[float] = None,
    vorp82: Optional[float] = None,
) -> PlayerMetricSeason:
    """Build a season carrying only the fields the rollup reads."""
    return PlayerMetricSeason(
        year=2025,
        venue_slug="las_vegas",
        venue_label="Las Vegas",
        venue_abbr="LV",
        gp=3,
        minutes=minutes,
        pts=pts,
        fga=fga,
        fta=fta,
        tov=tov,
        ts_pct=ts,
        efg_pct=None,
        gmsc=None,
        ftr=None,
        usg_pct=None,
        ast_pct=None,
        tov_pct=None,
        trb_pct=None,
        per=per,
        ortg=None,
        drtg=None,
        net_rtg=None,
        ws=ws,
        ws40=None,
        ws82=ws82,
        bpm=bpm,
        vorp=vorp,
        vorp82=vorp82,
    )


def test_resolve_competition_exact_match() -> None:
    """An exact (year, venue) pair is honored."""
    assert _resolve_competition(_COMPS, 2024, "las_vegas") == (2024, "las_vegas")


def test_resolve_competition_year_fallback() -> None:
    """An unavailable venue for a requested year falls back to that year's first."""
    assert _resolve_competition(_COMPS, 2025, "orlando") == (2025, "las_vegas")


def test_resolve_competition_venue_fallback() -> None:
    """A venue with no requested year falls back to its latest year."""
    assert _resolve_competition(_COMPS, None, "salt_lake_city") == (
        2025,
        "salt_lake_city",
    )


def test_resolve_competition_defaults_to_latest() -> None:
    """No request resolves to the newest competition; empty list yields None."""
    assert _resolve_competition(_COMPS, None, None) == (2025, "las_vegas")
    assert _resolve_competition([], 2025, "las_vegas") is None


def test_weighted_mean_weights_by_minutes() -> None:
    """A minute-weighted mean leans toward the higher-minute value."""
    result = _weighted_mean([(10.0, 100.0), (20.0, 300.0)])
    # (10*100 + 20*300) / 400 = 17.5
    assert result == 17.5


def test_weighted_mean_skips_none_values() -> None:
    """``None`` values contribute no weight rather than counting as zero."""
    result = _weighted_mean([(None, 100.0), (20.0, 100.0)])
    assert result == 20.0


def test_weighted_mean_empty_is_none() -> None:
    """No usable pairs yields ``None`` (not a divide-by-zero)."""
    assert _weighted_mean([]) is None
    assert _weighted_mean([(None, 50.0)]) is None
    assert _weighted_mean([(5.0, 0.0)]) is None


def test_career_sums_additive_shares() -> None:
    """Cumulative WS/VORP are summed; PER/BPM and the /82 rates are weighted."""
    seasons = [
        _season(
            minutes=100.0, per=18.0, pts=120, fga=90, fta=25, bpm=4.0,
            ws=1.0, vorp=0.6, ws82=8.0, vorp82=6.0,
        ),
        _season(
            minutes=300.0, per=22.0, pts=300, fga=210, fta=50, bpm=8.0,
            ws=2.0, vorp=1.4, ws82=12.0, vorp82=10.0,
        ),
    ]
    career = _career(seasons)

    assert career.adv_pools == 2
    assert career.gp == 6
    assert career.minutes == 400.0
    assert career.ws == 3.0  # 1.0 + 2.0 (cumulative, summed)
    assert career.vorp == 2.0  # 0.6 + 1.4 (cumulative, summed)
    # Minute-weighted: (18*100 + 22*300)/400 = 21.0 ; (4*100 + 8*300)/400 = 7.0
    assert career.per_avg == 21.0
    assert career.bpm_avg == 7.0
    # TS% pools shot totals, not minutes: PTS 420 / (2 * (FGA 300 + 0.44 * FTA 75))
    # = 420 / 666 = 63.1 (a minute-weighted mean would misstate it).
    assert career.ts_avg == 63.1
    # Projections are minute-weighted, not summed:
    # (8*100 + 12*300)/400 = 11.0 ; (6*100 + 10*300)/400 = 9.0
    assert career.ws82_avg == 11.0
    assert career.vorp82_avg == 9.0
    # WS/40 recomputed from summed shares: 3.0 / 400 * 40 = 0.3
    assert career.ws40 == 0.3


def test_career_pools_attempt_and_turnover_rates_from_volume() -> None:
    """Career FTr/TOV% recompute from summed box totals, not averaged rates."""
    seasons = [
        _season(minutes=100.0, pts=120, fga=100, fta=20, tov=10),
        _season(minutes=300.0, pts=300, fga=200, fta=70, tov=20),
    ]
    career = _career(seasons)
    # FTr = 90 / 300 = 0.3 (a fraction, like the stored per-pool column).
    assert career.ftr == 0.3
    # TOV% = 100 * 30 / (300 + 0.44*90 + 30) = 3000/369.6 = 8.1
    assert career.tov_pct == 8.1


def test_career_none_composites_stay_none() -> None:
    """A career with no composite inputs reports ``None``, not 0.0."""
    career = _career([_season(minutes=120.0), _season(minutes=80.0)])
    assert career.ws is None
    assert career.vorp is None
    assert career.ws40 is None
    assert career.per_avg is None
    assert career.ts_avg is None  # no shot attempts to pool
    assert career.bpm_avg is None
    assert career.ws82_avg is None
    assert career.vorp82_avg is None
    assert career.ftr is None  # no attempts / plays to pool
    assert career.tov_pct is None
    assert career.minutes == 200.0

"""Unit coverage for versioned Summer League metric publication."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.player_affiliation import AffiliationStatus
from app.services.summer_league import desk_read, metrics
from app.services.summer_league_explorer_service import ExplorerQuery


def _context(competition_id: int, *, eligible: bool = True) -> SimpleNamespace:
    """Build the context-shaped values consumed by the persistence seam."""
    return SimpleNamespace(
        competition_id=competition_id,
        year=2026,
        venue="las-vegas",
        pace=100.0,
        pts_per_poss=1.1,
        ppg=90.0,
        factor=0.5,
        vop=1.0,
        drb_pct=0.7,
        aper_scalar=1.2,
        team_games=4,
        complete_games=4,
        adv_eligible=eligible,
    )


def _season(competition_id: int) -> metrics.PlayerSeason:
    """Build a minimal season aggregate that exercises row projection."""
    return metrics.PlayerSeason(
        player_id=17,
        competition_id=competition_id,
        primary_team_entry_id=23,
        year=2026,
        venue="las-vegas",
        box=metrics.Box(mp=20.0, gp=2, pts=18.0, fgm=7.0, fga=14.0),
        team=metrics.Box(),
        opp=metrics.Box(),
        pm=2.0,
    )


def _result(*, seasons: list[metrics.PlayerSeason]) -> SimpleNamespace:
    """Build the compute result shape without invoking SQL-backed computation."""
    return SimpleNamespace(
        contexts={1: _context(1), 2: _context(2, eligible=False)},
        seasons=seasons,
        pyth_exponent=1.2,
        pyth_n=12,
        ws_ppw_coeff=3.3,
        bpm_coef={"fg2m": 0.4},
        bpm_intercept=0.1,
        bpm_r2=0.8,
        bpm_n_fit=20,
        shot_diet={},
        assisted_fg={},
        competition_trend_bands={},
        season_trend_bands={},
        season_trend_as_of=datetime(2026, 7, 28, 12, 0),
        as_of=datetime(2026, 7, 28, 12, 0),
    )


@pytest.mark.asyncio
async def test_source_as_of_uses_the_latest_metric_input_timestamp() -> None:
    """The projection watermark is the maximum timestamp across all source feeds."""
    db = MagicMock()
    first = datetime(2026, 7, 27, 10, 0)
    latest = datetime(2026, 7, 28, 11, 0)
    db.scalar = AsyncMock(side_effect=[first, None, latest, None, None])

    assert await metrics._source_as_of(db) == latest


@pytest.mark.asyncio
async def test_metric_build_uses_a_repeatable_read_snapshot() -> None:
    """Unlocked rebuilds pin reads and survive the role idle timeout during fitting."""
    db = MagicMock()
    db.execute = AsyncMock()

    await metrics.set_repeatable_read_snapshot(db)

    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "SET LOCAL idle_in_transaction_session_timeout = 0",
    ]


@pytest.mark.asyncio
async def test_rebuild_staged_writes_an_inactive_candidate_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staging persists contexts and seasons without publishing their pointer."""
    result = _result(seasons=[_season(1)])
    compute = AsyncMock(return_value=result)
    next_version = AsyncMock(return_value=7)
    publish_model = AsyncMock()
    monkeypatch.setattr(metrics, "compute", compute)
    monkeypatch.setattr(metrics, "next_metric_version", next_version)
    monkeypatch.setattr(metrics, "publish_metric_model", publish_model)
    idle_timeout = AsyncMock()
    monkeypatch.setattr(metrics, "set_rebuild_idle_timeout", idle_timeout)

    db = MagicMock()
    effective_day = date(2026, 7, 28)
    summary = await metrics.rebuild_staged(
        db, model_version="candidate", effective_day=effective_day
    )

    assert summary == {
        "seasons": 1,
        "contexts": 2,
        "adv_pools": 1,
        "version": 7,
        "model_version": "candidate",
        "as_of": datetime(2026, 7, 28, 12, 0),
        "effective_day": effective_day,
        "published": False,
    }
    publish_model.assert_awaited_once_with(
        db, version="candidate", result=result, activate=False
    )
    idle_timeout.assert_awaited_once_with(db)
    assert all(call.args[0].is_current is False for call in db.add.call_args_list)


@pytest.mark.asyncio
async def test_scoped_rebuild_publishes_only_the_requested_candidate_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped rebuild reuses the active fit and flips only selected pools."""
    result = _result(seasons=[_season(1), _season(2)])
    monkeypatch.setattr(metrics, "compute", AsyncMock(return_value=result))
    monkeypatch.setattr(metrics, "next_metric_version", AsyncMock(return_value=8))
    monkeypatch.setattr(
        metrics, "_active_or_fresh_model_version", AsyncMock(return_value="active-fit")
    )
    idle_timeout = AsyncMock()
    monkeypatch.setattr(metrics, "set_rebuild_idle_timeout", idle_timeout)
    publish_version = AsyncMock()
    monkeypatch.setattr(metrics, "publish_metric_version", publish_version)

    db = MagicMock()
    effective_day = date(2026, 7, 28)
    summary = await metrics.rebuild(
        db, competition_ids=[1], effective_day=effective_day
    )

    assert summary["seasons"] == 1
    assert summary["contexts"] == 1
    assert summary["version"] == 8
    assert summary["model_version"] == "active-fit"
    publish_version.assert_awaited_once_with(
        db,
        version=8,
        competition_ids=frozenset({1}),
        model_version=None,
        as_of=datetime(2026, 7, 28, 12, 0),
        effective_day=effective_day,
    )
    idle_timeout.assert_awaited_once_with(db)
    assert db.add.call_count == 2


@pytest.mark.asyncio
async def test_class_tracker_reads_current_and_candidate_metric_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tracker selects current rows by default and an exact candidate when requested."""
    player = SimpleNamespace(
        id=17,
        draft_year=2026,
        draft_round=1,
        draft_pick=5,
        display_name="Test Player",
        position="G",
    )
    roster_result = SimpleNamespace(all=lambda: [(17, 23, AffiliationStatus.ACTIVE)])
    players_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [player])
    )
    season_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    monkeypatch.setattr(desk_read, "_fetch_team_entries", AsyncMock(return_value={}))

    async def assemble(metrics_version: int | None) -> None:
        """Run one tracker query using the requested pointer mode."""
        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[roster_result, players_result, season_result]
        )
        section, _teams = await desk_read._assemble_tracker(
            db,
            competition_ids=[1],
            event_year=2026,
            baseline_version=None,
            cohort="full_class",
            stat_view="box",
            metrics_version=metrics_version,
        )
        assert len(section.rows) == 1
        assert db.execute.await_count == 3

    await assemble(None)
    await assemble(9)


@pytest.mark.asyncio
async def test_explorer_adv_counts_filter_current_contexts() -> None:
    """Explorer eligibility counts are sourced from the current context projection."""
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(one=lambda: (3, 2)))

    from app.services.summer_league_explorer_service import _fetch_adv_counts

    assert await _fetch_adv_counts(db, ExplorerQuery()) == (2, 3)

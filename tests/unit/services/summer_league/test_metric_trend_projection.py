"""Unit coverage for offline Summer League trend-band materialization."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.stats.inputs import PlayerSeason, StatInputs
from app.services.sources.summer_league.metric_trend_projection import (
    materialize_scoped_season_trend_bands,
    materialize_trend_bands,
)


def _season(
    player_id: int, competition_id: int, year: int, gmsc: float
) -> PlayerSeason:
    """Build one projected player-season with a trend metric."""
    season = PlayerSeason(
        player_id=player_id,
        competition_id=competition_id,
        primary_team_entry_id=None,
        year=year,
        venue="las_vegas",
        box=StatInputs(),
        team=StatInputs(),
        opp=StatInputs(),
    )
    season.metrics["gmsc"] = gmsc
    return season


def test_materialize_trend_bands_builds_competition_and_season_scopes() -> None:
    """Offline projection stores exact bands at both public trend scopes."""
    competition, season = materialize_trend_bands(
        [_season(1, 10, 2026, 4.0), _season(2, 11, 2026, 8.0)],
        include_season_scope=True,
    )

    assert competition[10]["gmsc"] == {"median": 4.0, "q1": 4.0, "q3": 4.0}
    assert season[2026]["gmsc"] == {"median": 6.0, "q1": 5.0, "q3": 7.0}


def test_scoped_live_projection_omits_partial_season_band() -> None:
    """A competition-only live tick cannot publish a misleading season cohort."""
    _competition, season = materialize_trend_bands(
        [_season(1, 10, 2026, 4.0)],
        include_season_scope=False,
    )

    assert season == {}


@pytest.mark.asyncio
async def test_scoped_season_band_merges_other_current_competitions() -> None:
    """A scoped live tick retains an accurate year-wide materialized band."""
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                year=2026,
                gmsc=8.0,
                ts_pct=None,
                bpm=None,
                as_of=datetime(2026, 7, 10, 10),
            )
        ]
    )

    projection = await materialize_scoped_season_trend_bands(
        db,
        [_season(1, 10, 2026, 4.0)],
        scoped_competition_ids=frozenset({10}),
        scoped_as_of=datetime(2026, 7, 10, 12),
    )

    assert projection.bands[2026]["gmsc"] == {
        "median": 6.0,
        "q1": 5.0,
        "q3": 7.0,
    }
    assert projection.as_of == datetime(2026, 7, 10, 10)

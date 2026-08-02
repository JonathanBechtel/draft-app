"""Integration coverage for version-coherent Summer League daily trends."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueCompetition
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.metric_trends import get_daily_trend
from app.services.share_cards.model_builders import build_sl_trend_model
from tests.integration.conftest import make_player


@pytest.mark.asyncio
async def test_trend_does_not_mix_partial_later_version_with_older_cohort(
    db_session: AsyncSession,
) -> None:
    """A later partial publication wins the day, so stale players are excluded."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="trend-coherent",
        venue_slug="las_vegas",
        display_name="Trend Coherent",
    )
    player_one = make_player("Trend", "One")
    player_two = make_player("Trend", "Two")
    db_session.add_all([competition, player_one, player_two])
    await db_session.flush()
    assert competition.id is not None
    assert player_one.id is not None
    assert player_two.id is not None

    day = date(2026, 7, 12)
    db_session.add_all(
        [
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player_one.id,
                year=2026,
                venue_slug="las_vegas",
                version=1,
                gmsc=4.0,
                effective_day=day,
                as_of=datetime(2026, 8, 1, 10),
                published_at=datetime(2026, 8, 1, 11),
            ),
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player_two.id,
                year=2026,
                venue_slug="las_vegas",
                version=1,
                gmsc=8.0,
                effective_day=day,
                as_of=datetime(2026, 8, 1, 10),
                published_at=datetime(2026, 8, 1, 11),
            ),
            # Version 2 is intentionally partial: player two has no row.
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player_one.id,
                year=2026,
                venue_slug="las_vegas",
                version=2,
                gmsc=10.0,
                effective_day=day,
                as_of=datetime(2026, 8, 1, 12),
                published_at=datetime(2026, 8, 1, 13),
            ),
        ]
    )
    await db_session.commit()

    points = await get_daily_trend(
        db_session,
        scope_key=f"competition:{competition.id}",
        player_id=None,
        metric_keys=("gmsc",),
    )

    assert len(points) == 1
    assert points[0].effective_day == day
    assert points[0].value == 10.0
    assert points[0].cohort_band.median == 10.0
    assert points[0].cohort_band.q1 == 10.0
    assert points[0].cohort_band.q3 == 10.0


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_trend_route_exposes_response_model_and_deterministic_payload(
    db_session: AsyncSession,
    app_client: AsyncClient,
) -> None:
    """The public route returns the typed trend point shape in HTTP order."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="trend-route",
        venue_slug="las_vegas",
        display_name="Trend Route",
    )
    player = make_player("Route", "Trend")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None
    assert player.id is not None
    db_session.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=2026,
            venue_slug="las_vegas",
            version=1,
            gmsc=7.5,
            effective_day=date(2026, 7, 13),
            as_of=datetime(2026, 8, 1, 12),
            published_at=datetime(2026, 8, 1, 13),
        )
    )
    await db_session.commit()

    response = await app_client.get(
        "/api/summer-league/trends",
        params={
            "scope_key": f"competition:{competition.id}",
            "player_id": player.id,
            "metric_keys": "gmsc",
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["metric_key"] == "gmsc"
    assert response.json()[0]["effective_day"] == "2026-07-13"
    assert response.json()[0]["value"] == 7.5


@pytest.mark.asyncio
async def test_season_scope_chooses_one_version_for_all_competitions(
    db_session: AsyncSession,
) -> None:
    """Season trends do not leak an older competition row into a winning close."""
    competition_a = SummerLeagueCompetition(
        year=2026,
        league_id="trend-season-a",
        venue_slug="las_vegas",
        display_name="Trend Season A",
    )
    competition_b = SummerLeagueCompetition(
        year=2026,
        league_id="trend-season-b",
        venue_slug="sacramento",
        display_name="Trend Season B",
    )
    player_a = make_player("Season", "A")
    player_b = make_player("Season", "B")
    db_session.add_all([competition_a, competition_b, player_a, player_b])
    await db_session.flush()
    assert competition_a.id and competition_b.id and player_a.id and player_b.id
    day = date(2026, 7, 14)
    db_session.add_all(
        [
            SummerLeaguePlayerSeason(
                competition_id=competition_a.id,
                player_id=player_a.id,
                year=2026,
                venue_slug="las_vegas",
                version=1,
                gmsc=1.0,
                effective_day=day,
                published_at=datetime(2026, 8, 1, 11),
            ),
            SummerLeaguePlayerSeason(
                competition_id=competition_b.id,
                player_id=player_b.id,
                year=2026,
                venue_slug="sacramento",
                version=1,
                gmsc=2.0,
                effective_day=day,
                published_at=datetime(2026, 8, 1, 11),
            ),
            SummerLeaguePlayerSeason(
                competition_id=competition_a.id,
                player_id=player_a.id,
                year=2026,
                venue_slug="las_vegas",
                version=2,
                gmsc=10.0,
                effective_day=day,
                published_at=datetime(2026, 8, 1, 12),
            ),
        ]
    )
    await db_session.commit()

    points = await get_daily_trend(
        db_session,
        scope_key="season:2026",
        player_id=None,
        metric_keys=("gmsc",),
    )

    assert len(points) == 1
    assert points[0].value == 10.0
    assert points[0].cohort_band.median == 10.0


@pytest.mark.asyncio
async def test_trend_share_model_reads_real_daily_close_rows(
    db_session: AsyncSession,
) -> None:
    """The share-card model uses the same published trend read as the page."""
    competition = SummerLeagueCompetition(
        year=2024,
        league_id="trend-share-model",
        venue_slug="las_vegas",
        display_name="Trend Share Model",
    )
    player = make_player("Share", "Trend")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id and player.id
    db_session.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=2024,
            venue_slug="las_vegas",
            version=1,
            gmsc=7.0,
            ts_pct=0.55,
            bpm=1.0,
            effective_day=date(2024, 7, 10),
            as_of=datetime(2026, 7, 20, 12),
            published_at=datetime(2026, 7, 20, 13),
        )
    )
    await db_session.commit()

    model = await build_sl_trend_model(
        db_session,
        [player.id],
        {
            "scope_key": f"competition:{competition.id}",
            "metric_keys": ["gmsc", "ts_pct", "bpm"],
        },
    )

    assert model.single_point is True
    assert {line.key for line in model.lines} == {"gmsc", "ts_pct", "bpm"}
    assert model.as_of == "2026-07-20"

"""Real-Postgres coverage for the non-promoting archival publisher."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.metric_publish import (
    publish_archival_metric_version,
)
from app.services.summer_league_metrics_service import get_player_metric_seasons
from scripts.backfill_sl_daily_trend_versions import run_backfill
from tests.integration.conftest import make_player


@pytest.mark.asyncio
async def test_archival_publish_cannot_demote_current_rows_or_change_reader_state(  # noqa: PLR0915
    db_session: AsyncSession,
) -> None:
    """Archival stamping leaves the current pointer and its values byte-for-byte intact."""
    competition = SummerLeagueCompetition(
        year=2017,
        league_id="archive-cannot-demote",
        venue_slug="las_vegas",
        display_name="Archive Cannot Demote",
    )
    player = make_player("Archive", "Current")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None and player.id is not None
    player_id = player.id

    current_day = date(2017, 7, 8)
    source_watermark = datetime(2026, 8, 1, 12)
    current_context = SummerLeagueMetricContext(
        competition_id=competition.id,
        year=2017,
        venue_slug="las_vegas",
        version=7,
        is_current=True,
        effective_day=current_day,
        published_at=datetime(2026, 8, 1, 13),
        as_of=source_watermark,
    )
    current_season = SummerLeaguePlayerSeason(
        competition_id=competition.id,
        player_id=player.id,
        year=2017,
        venue_slug="las_vegas",
        version=7,
        is_current=True,
        effective_day=current_day,
        published_at=datetime(2026, 8, 1, 13),
        as_of=source_watermark,
        gp=1,
        gmsc=12.5,
        minutes=45.0,
        adv_eligible=True,
    )
    archival_context = SummerLeagueMetricContext(
        competition_id=competition.id,
        year=2017,
        venue_slug="las_vegas",
        version=99,
        is_current=False,
        effective_day=date(2017, 7, 9),
    )
    archival_season = SummerLeaguePlayerSeason(
        competition_id=competition.id,
        player_id=player.id,
        year=2017,
        venue_slug="las_vegas",
        version=99,
        is_current=False,
        effective_day=date(2017, 7, 9),
        gp=2,
        gmsc=18.0,
    )
    db_session.add_all(
        [current_context, current_season, archival_context, archival_season]
    )
    await db_session.flush()
    assert (
        current_context.id is not None
        and current_season.id is not None
        and archival_season.id is not None
    )
    current_context_id = current_context.id
    current_season_id = current_season.id
    archival_season_id = archival_season.id
    competition_id = competition.id
    before = (
        current_context.version,
        current_context.is_current,
        current_season.version,
        current_season.is_current,
        current_season.gmsc,
    )
    await db_session.commit()
    before_reader = await get_player_metric_seasons(db_session, player_id)
    assert before_reader is not None
    before_reader_values = [
        (season.year, season.gp, season.minutes, season.gmsc)
        for season in before_reader.seasons
    ]
    first = await publish_archival_metric_version(
        db_session,
        version=99,
        competition_ids={competition_id},
        as_of=source_watermark,
        effective_day=date(2017, 7, 9),
    )
    await db_session.commit()
    assert first.contexts == 1
    assert first.seasons == 1

    db_session.expire_all()
    current_context_after = await db_session.get(
        SummerLeagueMetricContext, current_context_id
    )
    current_season_after = await db_session.get(
        SummerLeaguePlayerSeason, current_season_id
    )
    assert current_context_after is not None and current_season_after is not None
    after = (
        current_context_after.version,
        current_context_after.is_current,
        current_season_after.version,
        current_season_after.is_current,
        current_season_after.gmsc,
    )
    assert after == before
    after_reader = await get_player_metric_seasons(db_session, player_id)
    assert after_reader is not None
    assert [
        (season.year, season.gp, season.minutes, season.gmsc)
        for season in after_reader.seasons
    ] == before_reader_values

    with pytest.raises(ValueError, match="contains current rows"):
        await publish_archival_metric_version(
            db_session,
            version=7,
            competition_ids={competition_id},
            as_of=source_watermark,
            effective_day=current_day,
        )
    await db_session.rollback()

    archival_rows = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.version == 99
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(archival_rows) == 1
    assert archival_rows[0].is_current is False
    assert archival_rows[0].published_at is not None

    published_at = archival_rows[0].published_at
    second = await publish_archival_metric_version(
        db_session,
        version=99,
        competition_ids={competition_id},
        as_of=source_watermark,
        effective_day=date(2017, 7, 9),
    )
    await db_session.commit()
    assert second.contexts == 0
    assert second.seasons == 0
    db_session.expire_all()
    archival_after = await db_session.get(SummerLeaguePlayerSeason, archival_season_id)
    assert archival_after is not None
    assert archival_after.published_at == published_at
    assert archival_after.is_current is False


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_archival_rows_feed_the_public_trend_endpoint(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """A published archival season is returned by the existing trend API."""
    competition = SummerLeagueCompetition(
        year=2018,
        league_id="archive-trend-endpoint",
        venue_slug="las_vegas",
        display_name="Archive Trend Endpoint",
    )
    player = make_player("Archive", "Trend")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None and player.id is not None
    competition_id = competition.id
    player_id = player.id
    day = date(2018, 7, 9)
    db_session.add(
        SummerLeaguePlayerSeason(
            competition_id=competition_id,
            player_id=player_id,
            year=2018,
            venue_slug="las_vegas",
            version=123,
            is_current=False,
            effective_day=day,
            gp=1,
            gmsc=9.25,
        )
    )
    await db_session.flush()
    publication = await publish_archival_metric_version(
        db_session,
        version=123,
        competition_ids={competition_id},
        as_of=datetime(2026, 8, 1, 12),
        effective_day=day,
    )
    await db_session.commit()
    assert publication.seasons == 1

    response = await app_client.get(
        "/api/summer-league/trends",
        params={
            "scope_key": f"competition:{competition_id}",
            "player_id": player_id,
            "metric_keys": "gmsc",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["effective_day"] == "2018-07-09"
    assert payload[0]["value"] == 9.25


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_backfill_two_events_is_idempotent_and_trend_endpoint_reads_both(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """Two seeded historical events archive on the first run and no-op on retry."""
    players = [make_player("Backfill", "One"), make_player("Backfill", "Two")]
    competitions: list[SummerLeagueCompetition] = []
    first_competition_id: int | None = None
    first_player_id: int | None = None
    for player, year in zip(players, (2019, 2020), strict=True):
        competition = SummerLeagueCompetition(
            year=year,
            league_id=f"archive-seeded-{year}",
            venue_slug="las_vegas",
            display_name=f"Archive Seeded {year}",
        )
        db_session.add_all([competition, player])
        competitions.append(competition)
        await db_session.flush()
        assert competition.id is not None and player.id is not None
        if len(competitions) == 1:
            first_competition_id = competition.id
            first_player_id = player.id
        home = SummerLeagueTeamEntry(
            competition_id=competition.id,
            nba_stats_team_id=f"home-{year}",
            raw_team_name=f"Home {year}",
            team_slug=f"home-{year}",
        )
        away = SummerLeagueTeamEntry(
            competition_id=competition.id,
            nba_stats_team_id=f"away-{year}",
            raw_team_name=f"Away {year}",
            team_slug=f"away-{year}",
        )
        db_session.add_all([home, away])
        await db_session.flush()
        game = SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id=f"game-{year}",
            game_date=date(year, 7, 9),
            home_team_entry_id=home.id,
            away_team_entry_id=away.id,
            home_score=90,
            away_score=80,
            status=SummerLeagueGameStatus.FINAL,
        )
        source_player = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"person-{year}",
            raw_player_name=f"Backfill {year}",
            normalized_name=f"backfill-{year}",
        )
        db_session.add_all([game, source_player])
        await db_session.flush()
        assert game.id is not None and source_player.id is not None
        db_session.add_all(
            [
                SummerLeagueTeamGameLog(
                    competition_id=competition.id,
                    game_id=game.id,
                    team_entry_id=home.id,
                    minutes=200,
                    pts=90,
                    fgm=30,
                    fga=70,
                    ftm=20,
                    fta=25,
                    reb=40,
                ),
                SummerLeagueTeamGameLog(
                    competition_id=competition.id,
                    game_id=game.id,
                    team_entry_id=away.id,
                    minutes=200,
                    pts=80,
                    fgm=28,
                    fga=70,
                    ftm=18,
                    fta=25,
                    reb=38,
                ),
                SummerLeaguePlayerGameLog(
                    competition_id=competition.id,
                    game_id=game.id,
                    team_entry_id=home.id,
                    source_player_id=source_player.id,
                    player_id=player.id,
                    nba_stats_person_id=source_player.nba_stats_person_id,
                    raw_player_name=source_player.raw_player_name,
                    minutes_seconds=2400,
                    pts=20,
                    fgm=8,
                    fga=15,
                    ftm=4,
                    fta=5,
                    reb=8,
                ),
            ]
        )
    await db_session.commit()

    first = await run_backfill(db_session)
    assert first.planned == 2
    assert first.archived == 2
    assert first.contexts == 2
    assert first.seasons == 2
    await db_session.commit()

    second = await run_backfill(db_session)
    assert second.planned == 2
    assert second.archived == 0
    assert second.skipped == 2
    assert second.contexts == 0
    assert second.seasons == 0

    assert first_competition_id is not None and first_player_id is not None
    response = await app_client.get(
        "/api/summer-league/trends",
        params={
            "scope_key": f"competition:{first_competition_id}",
            "player_id": first_player_id,
            "metric_keys": "gmsc",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload and payload[0]["effective_day"] == "2019-07-09"

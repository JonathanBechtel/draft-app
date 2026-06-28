"""Integration tests for the player SL shot-chart + shot-diet wiring.

Exercises two surfaces:
  - GET /players/{slug}        — career shot-chart context in the SL section
  - GET /players/{slug}/summer-league/{year} — per-season page

Cases:
  1. Player with resolved shot events + SummerLeaguePlayerSeason  → chart rendered.
  2. Player with game logs but no shot events                     → graceful empty.
  3. Player with no SL data at all                                → section absent.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueShotEvent,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from tests.integration.conftest import make_player

# ---------------------------------------------------------------------------
# Unique-ID counter to avoid PK / unique-constraint collisions across tests
# ---------------------------------------------------------------------------
_SEQ: dict[str, int] = {"n": 0}


def _uid() -> str:
    _SEQ["n"] += 1
    return f"sc-{_SEQ['n']}"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _make_competition(
    db: AsyncSession, *, year: int, venue_slug: str = "las_vegas"
) -> SummerLeagueCompetition:
    existing = (
        await db.execute(
            select(SummerLeagueCompetition).where(  # type: ignore[call-overload]
                SummerLeagueCompetition.year == year,
                SummerLeagueCompetition.venue_slug == venue_slug,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    comp = SummerLeagueCompetition(
        year=year,
        league_id=f"15-{_uid()}",
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug} Summer League",
    )
    db.add(comp)
    await db.flush()
    return comp


async def _make_team(
    db: AsyncSession, *, comp_id: int
) -> SummerLeagueTeamEntry:
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=_uid(),
        raw_team_name=f"Team {_uid()}",
        raw_team_abbreviation="TST",
        team_slug=f"test-{_uid()}",
    )
    db.add(team)
    await db.flush()
    return team


async def _make_source_player(
    db: AsyncSession, *, player: PlayerMaster
) -> SummerLeagueSourcePlayer:
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=_uid(),
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()
    return sp


async def _make_game_and_log(
    db: AsyncSession,
    *,
    player: PlayerMaster,
    source_player: SummerLeagueSourcePlayer,
    comp: SummerLeagueCompetition,
    game_date: date,
) -> SummerLeagueGame:
    """Seed one game + log for a player."""
    assert comp.id is not None
    home = await _make_team(db, comp_id=comp.id)
    away = await _make_team(db, comp_id=comp.id)
    assert home.id is not None and away.id is not None

    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=_uid(),
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=110,
        away_score=95,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None

    log = SummerLeaguePlayerGameLog(
        competition_id=comp.id,
        game_id=game.id,
        team_entry_id=home.id,
        source_player_id=source_player.id,
        player_id=player.id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        raw_player_name=player.display_name or "Player",
        minutes_seconds=1800,
        pts=20,
        reb=8,
        ast=5,
        fgm=8,
        fga=16,
        fg3m=2,
        fg3a=6,
        ftm=2,
        fta=2,
    )
    db.add(log)
    await db.flush()
    return game


def _make_shot_event(
    *,
    player: PlayerMaster,
    source_player: SummerLeagueSourcePlayer,
    game: SummerLeagueGame,
    comp_id: int,
    team_entry_id: int,
    zone: str,
    made: bool,
    event_num: int,
) -> SummerLeagueShotEvent:
    return SummerLeagueShotEvent(
        game_id=game.id,
        competition_id=comp_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player.id,
        player_id=player.id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        nba_stats_game_id=game.nba_stats_game_id,
        nba_stats_game_event_id=event_num,
        shot_zone_basic=zone,
        loc_x=100,
        loc_y=200,
        made=made,
    )


async def _seed_rich_player(
    db: AsyncSession,
    *,
    year: int = 2024,
) -> tuple[PlayerMaster, SummerLeagueCompetition]:
    """Seed a player with ≥20 shot events and a SummerLeaguePlayerSeason row."""
    player = make_player("Shot", "Charter", school="Duke")
    db.add(player)
    await db.flush()
    await db.refresh(player)
    assert player.id is not None

    sp = await _make_source_player(db, player=player)
    comp = await _make_competition(db, year=year, venue_slug="las_vegas")
    assert comp.id is not None
    game = await _make_game_and_log(
        db, player=player, source_player=sp, comp=comp, game_date=date(year, 7, 10)
    )
    assert game.id is not None

    # Seed 25 shot events across two zones (above the MIN_FGA_FOR_CHART=20
    # threshold so the chart is NOT suppressed).
    home_entry = (
        await db.execute(
            select(SummerLeagueTeamEntry).where(  # type: ignore[call-overload]
                SummerLeagueTeamEntry.competition_id == comp.id  # type: ignore[arg-type]
            )
        )
    ).scalars().first()
    assert home_entry is not None and home_entry.id is not None

    events = []
    for i in range(15):
        events.append(
            _make_shot_event(
                player=player,
                source_player=sp,
                game=game,
                comp_id=comp.id,
                team_entry_id=home_entry.id,
                zone="Restricted Area",
                made=(i % 2 == 0),
                event_num=i + 1,
            )
        )
    for i in range(10):
        events.append(
            _make_shot_event(
                player=player,
                source_player=sp,
                game=game,
                comp_id=comp.id,
                team_entry_id=home_entry.id,
                zone="Above the Break 3",
                made=(i % 3 == 0),
                event_num=100 + i,
            )
        )
    for ev in events:
        db.add(ev)
    await db.flush()

    # Seed a SummerLeaguePlayerSeason row with shot-diet rates.
    season_row = SummerLeaguePlayerSeason(
        competition_id=comp.id,
        player_id=player.id,
        year=year,
        venue_slug="las_vegas",
        gp=1,
        minutes=30.0,
        rim_rate=0.60,
        mid_rate=0.00,
        three_rate=0.40,
        corner3_rate=0.00,
    )
    db.add(season_row)
    await db.flush()

    return player, comp


# ---------------------------------------------------------------------------
# Tests: GET /players/{slug}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_detail_with_shots_shows_shotchart(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Player with ≥20 shot events: chart section and zone table appear on player detail."""
    player, _ = await _seed_rich_player(db_session)
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    html = resp.text

    # window.SL_SHOTCHART is injected and non-null.
    assert "window.SL_SHOTCHART" in html
    assert "SL_SHOTCHART = null" not in html

    # Chart mount point present.
    assert 'id="sl-shotchart-root"' in html

    # Zone table rendered.
    assert "sl-shotchart-zone-table" in html
    assert "Restricted Area" in html

    # Shot-diet row rendered.
    assert "sl-shot-diet" in html
    assert "Rim" in html
    assert "3PT" in html

    # JS loaded conditionally.
    assert "summer-league-shotchart.js" in html


@pytest.mark.asyncio
async def test_player_detail_no_shots_omits_shotchart(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Player with game logs but no shot events: shot-chart block absent."""
    player = make_player("No", "Shots", school="Kentucky")
    db_session.add(player)
    await db_session.flush()
    await db_session.refresh(player)

    sp = await _make_source_player(db_session, player=player)
    comp = await _make_competition(db_session, year=2024, venue_slug="salt_lake_city")
    await _make_game_and_log(
        db_session,
        player=player,
        source_player=sp,
        comp=comp,
        game_date=date(2024, 7, 8),
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    html = resp.text

    # SL section shows (game logs), but no shot chart.
    assert "summerLeagueSection" in html
    assert 'id="sl-shotchart-root"' not in html
    assert "SL_SHOTCHART = null" in html


@pytest.mark.asyncio
async def test_player_detail_no_sl_data_omits_all(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Player with no SL data: SL section absent, page 200s."""
    player = make_player("Off", "Season", school="Gonzaga")
    db_session.add(player)
    await db_session.flush()
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    html = resp.text

    assert "summerLeagueSection" not in html
    assert 'id="sl-shotchart-root"' not in html


# ---------------------------------------------------------------------------
# Tests: GET /players/{slug}/summer-league/{year}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_season_page_with_shots_shows_shotchart(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Per-season page: shot chart + diet row rendered for a player with data."""
    player, comp = await _seed_rich_player(db_session, year=2023)
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}/summer-league/2023")
    assert resp.status_code == 200
    html = resp.text

    # Chart elements present.
    assert 'id="sl-shotchart-root"' in html
    assert "sl-shotchart-zone-table" in html
    assert "window.SL_SHOTCHART" in html
    assert "SL_SHOTCHART = null" not in html

    # Shot-diet row.
    assert "sl-shot-diet" in html

    # Zone table data.
    assert "Restricted Area" in html
    assert "Above the Break 3" in html

    # Game log table present.
    assert "slg-logs-table" in html


@pytest.mark.asyncio
async def test_season_page_no_shots_shows_empty_state(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Per-season page: graceful empty state when no shot events exist."""
    player = make_player("Logs", "Only", school="UConn")
    db_session.add(player)
    await db_session.flush()
    await db_session.refresh(player)

    sp = await _make_source_player(db_session, player=player)
    comp = await _make_competition(db_session, year=2022, venue_slug="las_vegas")
    await _make_game_and_log(
        db_session,
        player=player,
        source_player=sp,
        comp=comp,
        game_date=date(2022, 7, 9),
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}/summer-league/2022")
    assert resp.status_code == 200
    html = resp.text

    # Game logs are present, chart is absent.
    assert "slg-logs-table" in html
    assert 'id="sl-shotchart-root"' not in html
    # Empty state message shown.
    assert "No shot-chart data available" in html


@pytest.mark.asyncio
async def test_season_page_unknown_player_returns_404(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Unknown slug returns 404."""
    resp = await app_client.get("/players/nobody-here/summer-league/2024")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_season_page_wrong_year_shows_no_logs(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Year with no logs for a real player: no games listed, no chart."""
    player, _ = await _seed_rich_player(db_session, year=2021)
    await db_session.commit()

    assert player.slug is not None
    # Request a year the player didn't play.
    resp = await app_client.get(f"/players/{player.slug}/summer-league/2019")
    assert resp.status_code == 200
    html = resp.text

    assert "hasn't played Summer League in 2019" in html
    assert 'id="sl-shotchart-root"' not in html


@pytest.mark.asyncio
async def test_competition_resolver_honors_venue(db_session: AsyncSession) -> None:
    """get_competition_id_for_player_year targets the clicked venue, else marquee.

    Regression for the per-season link: a player with multiple competitions in
    one year (Las Vegas + Salt Lake City) must resolve to the exact competition
    when a venue_slug is given, instead of always the marquee (Las Vegas) one.
    """
    from app.services.summer_league_stats_service import (
        get_competition_id_for_player_year,
    )

    player = make_player("Multi", "Venue", school="Duke")
    db_session.add(player)
    await db_session.flush()
    await db_session.refresh(player)
    assert player.id is not None

    lv = await _make_competition(db_session, year=2024, venue_slug="las_vegas")
    slc = await _make_competition(db_session, year=2024, venue_slug="salt_lake_city")
    for comp, venue in ((lv, "las_vegas"), (slc, "salt_lake_city")):
        assert comp.id is not None
        db_session.add(
            SummerLeaguePlayerSeason(
                competition_id=comp.id,
                player_id=player.id,
                year=2024,
                venue_slug=venue,
                gp=1,
                minutes=20.0,
            )
        )
    await db_session.flush()

    # Default → marquee (Las Vegas).
    assert await get_competition_id_for_player_year(db_session, player.id, 2024) == lv.id
    # Venue-scoped → the exact competition.
    assert (
        await get_competition_id_for_player_year(
            db_session, player.id, 2024, venue_slug="salt_lake_city"
        )
        == slc.id
    )

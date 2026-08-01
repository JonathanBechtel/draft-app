"""Integration tests for the player-detail Summer League section.

- Player WITH resolved Summer League logs: section renders with headline
  averages and a recent-games table; multi-venue logs in one year aggregate
  into a single season row.
- Player WITHOUT Summer League logs: section is entirely absent; page 200s.
- DNP logs are excluded from games/averages.
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
    SummerLeagueTeamGameLog,
)
from tests.integration.conftest import make_player

_COMP_SEQ = {"n": 0}


async def _get_or_create_competition(
    db: AsyncSession, *, year: int, league_id: str, venue_slug: str
) -> SummerLeagueCompetition:
    existing = (
        await db.execute(
            select(SummerLeagueCompetition).where(  # type: ignore[call-overload]
                SummerLeagueCompetition.year == year,
                SummerLeagueCompetition.league_id == league_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug} Summer League",
    )
    db.add(comp)
    await db.flush()
    return comp


async def _make_team(
    db: AsyncSession, *, comp_id: int, name: str, abbr: str
) -> SummerLeagueTeamEntry:
    _COMP_SEQ["n"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"team-{_COMP_SEQ['n']}",
        raw_team_name=name,
        raw_team_abbreviation=abbr,
        team_slug=f"{abbr.lower()}-{_COMP_SEQ['n']}",
    )
    db.add(team)
    await db.flush()
    return team


async def _make_source_player(
    db: AsyncSession, *, player: PlayerMaster
) -> SummerLeagueSourcePlayer:
    _COMP_SEQ["n"] += 1
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"person-{_COMP_SEQ['n']}",
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()
    return sp


async def _seed_game(
    db: AsyncSession,
    *,
    player: PlayerMaster,
    source_player: SummerLeagueSourcePlayer,
    year: int,
    league_id: str,
    venue_slug: str,
    game_date: date,
    minutes_seconds: int,
    pace: float | None,
    pts: int,
    reb: int,
    ast: int,
) -> None:
    """Seed one competition/game/team/log for ``player``."""
    _COMP_SEQ["n"] += 1
    comp = await _get_or_create_competition(
        db, year=year, league_id=league_id, venue_slug=venue_slug
    )
    assert comp.id is not None
    team = await _make_team(db, comp_id=comp.id, name="Home Team", abbr="HOM")
    opp = await _make_team(db, comp_id=comp.id, name="Las Vegas Aces", abbr="LVA")
    assert team.id is not None and opp.id is not None

    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"game-{_COMP_SEQ['n']}",
        game_date=game_date,
        home_team_entry_id=team.id,
        away_team_entry_id=opp.id,
        home_score=100,
        away_score=90,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None

    log = SummerLeaguePlayerGameLog(
        competition_id=comp.id,
        game_id=game.id,
        team_entry_id=team.id,
        source_player_id=source_player.id,
        player_id=player.id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        raw_player_name=player.display_name or "Player",
        minutes_seconds=minutes_seconds,
        pace=pace,
        pts=pts,
        reb=reb,
        ast=ast,
        fgm=pts // 2,
        fga=pts,
        fg3m=1,
        fg3a=3,
        ftm=2,
        fta=2,
        stl=1,
        blk=0,
        tov=2,
    )
    db.add(log)
    if pace is not None and minutes_seconds > 0:
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=team.id,
                minutes=180,
                fgm=50,
                fga=50,
                ftm=0,
                fta=0,
                oreb=0,
                dreb=0,
                tov=10,
            )
        )
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=opp.id,
                minutes=180,
                fgm=50,
                fga=50,
                ftm=0,
                fta=0,
                oreb=0,
                dreb=0,
                tov=10,
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_player_with_summer_league_shows_section(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Two venues in one year aggregate into one season; section renders."""
    player = make_player("Summer", "Leaguer", school="Duke")
    db_session.add(player)
    await db_session.flush()
    source_player = await _make_source_player(db_session, player=player)

    await _seed_game(
        db_session,
        player=player,
        source_player=source_player,
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2024, 7, 12),
        minutes_seconds=1800,
        pace=100.0,
        pts=20,
        reb=10,
        ast=4,
    )
    await _seed_game(
        db_session,
        player=player,
        source_player=source_player,
        year=2024,
        league_id="13",
        venue_slug="california_classic",
        game_date=date(2024, 7, 6),
        minutes_seconds=1200,
        pace=104.0,
        pts=10,
        reb=4,
        ast=2,
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    html = resp.text

    assert "summerLeagueSection" in html
    assert "Summer League" in html
    # Per-100 toggle is present (pace exists).
    assert 'data-sl-mode="per_100"' in html
    # window data injected (not null).
    assert "window.SUMMER_LEAGUE_DATA" in html
    assert "SUMMER_LEAGUE_DATA = null" not in html
    # Recent games table present.
    assert "Recent Games" in html

    # The service splits the two venues into two competition rows.
    from app.services.summer_league_stats_service import (
        get_summer_league_profile_by_player_id,
    )

    assert player.id is not None
    profile = await get_summer_league_profile_by_player_id(db_session, player.id)
    assert profile is not None
    assert len(profile.seasons) == 2
    # Both rows are 2024 but distinguished by league/venue abbreviation.
    assert {s.season_label for s in profile.seasons} == {"2024"}
    assert {s.venue_abbr for s in profile.seasons} == {"LV", "CC"}
    # Each row carries a single venue; Career combines both.
    assert all(len(s.venues) == 1 for s in profile.seasons)
    assert set(profile.career.venues) == {"Las Vegas", "California Classic"}


@pytest.mark.asyncio
async def test_player_without_summer_league_omits_section(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A player with no SL logs renders normally with the section absent."""
    player = make_player("No", "Summer", school="UNC")
    db_session.add(player)
    await db_session.flush()
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    html = resp.text
    assert "summerLeagueSection" not in html
    assert "SUMMER_LEAGUE_DATA = null" in html


@pytest.mark.asyncio
async def test_dnp_game_excluded_from_section(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A DNP log (0 minutes) does not count toward the displayed GP."""
    player = make_player("Dnp", "Player", school="Kentucky")
    db_session.add(player)
    await db_session.flush()
    source_player = await _make_source_player(db_session, player=player)

    await _seed_game(
        db_session,
        player=player,
        source_player=source_player,
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2025, 7, 12),
        minutes_seconds=1500,
        pace=99.0,
        pts=12,
        reb=5,
        ast=3,
    )
    await _seed_game(
        db_session,
        player=player,
        source_player=source_player,
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2025, 7, 13),
        minutes_seconds=0,
        pace=None,
        pts=0,
        reb=0,
        ast=0,
    )
    await db_session.commit()

    from app.services.summer_league_stats_service import (
        get_summer_league_profile_by_player_id,
    )

    assert player.id is not None
    profile = await get_summer_league_profile_by_player_id(db_session, player.id)
    assert profile is not None
    assert len(profile.seasons) == 1
    assert profile.seasons[0].gp == 1
    assert profile.seasons[0].modes["per_game"].pts == pytest.approx(12.0)

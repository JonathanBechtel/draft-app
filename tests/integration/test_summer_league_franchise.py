"""Integration tests for the Summer League franchise-history page.

Franchise (`/stats/summer-league/teams/{team}`): aggregates one NBA franchise's
SL entries across years/venues into an all-time record, by-season rows (each
linking to the team-season page), career leaders, and an all-players roster.
``{team}`` is the canonical ``nba_teams.slug``; non-franchise squads (null
``nba_team_id``) never get a page.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_franchise_service import get_franchise_history
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _entry(
    db: AsyncSession, *, comp_id: int, franchise: NbaTeam, name: str
) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    t = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=franchise.id,
        nba_stats_team_id=str(1610612747 + _N["i"]),
        raw_team_name=name,
        raw_team_abbreviation="LAL",
        team_slug=f"lakers-{_N['i']}",
    )
    db.add(t)
    await db.flush()
    return t


async def _game(
    db: AsyncSession,
    *,
    comp_id: int,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int,
    away_score: int,
    game_date: date,
    log_player: PlayerMaster,
    log_team: SummerLeagueTeamEntry,
    pts: int = 20,
) -> None:
    _N["i"] += 1
    g = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"fr-game-{_N['i']}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(g)
    await db.flush()
    assert g.id is not None
    sp = SummerLeagueSourceRecord(
        nba_stats_person_id=f"fr-person-{_N['i']}",
        raw_player_name=log_player.display_name or "Player",
        normalized_name=(log_player.display_name or "player").lower(),
        canonical_player_id=log_player.id,
    )
    db.add(sp)
    await db.flush()
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp_id,
            game_id=g.id,
            team_entry_id=log_team.id,
            source_player_id=sp.id,
            player_id=log_player.id,
            nba_stats_person_id=sp.nba_stats_person_id,
            raw_player_name=log_player.display_name or "Player",
            minutes_seconds=1800,
            pts=pts,
            reb=8,
            ast=5,
            fgm=8,
            fga=15,
        )
    )
    await db.flush()


async def _comp(db: AsyncSession, *, year: int, venue_slug: str, league_id: str) -> int:
    _N["i"] += 1
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp.id


async def _seed_franchise(db: AsyncSession) -> tuple[NbaTeam, PlayerMaster]:
    """Two SL years for one franchise: 2024 (1-1) and 2025 (1-0). Star scores big.

    All-time record should be 2-1. The star (40 PPG) leads career scoring.
    """
    franchise = NbaTeam(name="Los Angeles Lakers", abbreviation="LAL", slug="lakers")
    db.add(franchise)
    await db.flush()

    star = make_player("Star", "Wing")
    role = make_player("Role", "Player")
    db.add_all([star, role])
    await db.flush()

    # 2024 Vegas — Lakers split two games (1-1).
    c24 = await _comp(db, year=2024, venue_slug="vegas", league_id="15")
    lal24 = await _entry(db, comp_id=c24, franchise=franchise, name="Lakers")
    opp24 = await _entry(db, comp_id=c24, franchise=franchise, name="Foes")
    # opp24 is a *different* franchise in reality; reuse table but detach below.
    opp24.nba_team_id = None
    await db.flush()
    await _game(
        db,
        comp_id=c24,
        home=lal24,
        away=opp24,
        home_score=100,
        away_score=90,
        game_date=date(2024, 7, 3),
        log_player=star,
        log_team=lal24,
        pts=40,
    )
    await _game(
        db,
        comp_id=c24,
        home=opp24,
        away=lal24,
        home_score=99,
        away_score=88,
        game_date=date(2024, 7, 5),
        log_player=role,
        log_team=lal24,
        pts=10,
    )

    # 2025 Vegas — Lakers win (1-0).
    c25 = await _comp(db, year=2025, venue_slug="vegas", league_id="15")
    lal25 = await _entry(db, comp_id=c25, franchise=franchise, name="Lakers")
    opp25 = await _entry(db, comp_id=c25, franchise=franchise, name="Foes")
    opp25.nba_team_id = None
    await db.flush()
    await _game(
        db,
        comp_id=c25,
        home=lal25,
        away=opp25,
        home_score=110,
        away_score=95,
        game_date=date(2025, 7, 3),
        log_player=star,
        log_team=lal25,
        pts=40,
    )
    await db.commit()
    return franchise, star


@pytest.mark.asyncio
async def test_resolved_player_name_variants_collapse_to_one_row(
    db_session: AsyncSession,
) -> None:
    """A resolved player logged under two feed names aggregates into one row.

    Regression for the franchise aggregates splitting on ``raw_player_name``.
    """
    franchise = NbaTeam(name="Boston Celtics", abbreviation="BOS", slug="celtics")
    db_session.add(franchise)
    await db_session.flush()
    player = make_player("Jaylen", "Brown")
    db_session.add(player)
    await db_session.flush()

    comp_id = await _comp(db_session, year=2024, venue_slug="vegas", league_id="15")
    team = await _entry(db_session, comp_id=comp_id, franchise=franchise, name="Celtics")
    opp = await _entry(db_session, comp_id=comp_id, franchise=franchise, name="Foes")
    opp.nba_team_id = None
    await db_session.flush()

    # Same canonical player, two games, two different raw feed names.
    for i, raw_name in enumerate(("Jaylen Brown", "J. Brown")):
        g = SummerLeagueGame(
            competition_id=comp_id,
            nba_stats_game_id=f"var-game-{i}",
            game_date=date(2024, 7, 3 + i),
            home_team_entry_id=team.id,
            away_team_entry_id=opp.id,
            home_score=100,
            away_score=90,
        )
        db_session.add(g)
        await db_session.flush()
        sp = SummerLeagueSourceRecord(
            nba_stats_person_id=f"var-person-{i}",
            raw_player_name=raw_name,
            normalized_name=raw_name.lower(),
            canonical_player_id=player.id,
        )
        db_session.add(sp)
        await db_session.flush()
        db_session.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp_id,
                game_id=g.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                raw_player_name=raw_name,
                minutes_seconds=1800,
                pts=20,
            )
        )
    await db_session.commit()

    hist = await get_franchise_history(db_session, "celtics")

    assert hist is not None
    # One canonical player, not two — gp and points aggregated across both names.
    brown_rows = [p for p in hist.players if p.slug == player.slug]
    assert len(brown_rows) == 1
    assert brown_rows[0].gp == 2
    assert brown_rows[0].pts == 40
    assert hist.player_count == 1


@pytest.mark.asyncio
async def test_get_franchise_history_aggregates_record_and_players(
    db_session: AsyncSession,
) -> None:
    """All-time record sums across years; seasons sort newest-first; star leads."""
    franchise, star = await _seed_franchise(db_session)

    hist = await get_franchise_history(db_session, "lakers")

    assert hist is not None
    assert hist.name == "Los Angeles Lakers"
    assert (hist.all_time_wins, hist.all_time_losses) == (2, 1)
    assert hist.season_count == 2
    # Newest season first.
    assert [s.year for s in hist.seasons] == [2025, 2024]
    # By-season rows link via the per-competition team_slug, not the franchise.
    assert hist.seasons[0].team_slug.startswith("lakers-")
    # Star leads career scoring (40 + 40 = 80 pts over 2 GP).
    assert hist.leaders[0].slug == star.slug
    assert hist.leaders[0].pts == 80
    assert hist.leaders[0].seasons == 2
    # All players is alphabetical by name.
    names = [p.name for p in hist.players]
    assert names == sorted(names, key=str.lower)
    assert hist.player_count == 2


@pytest.mark.asyncio
async def test_franchise_page_renders(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """The franchise route renders the header, record, and a by-season link."""
    await _seed_franchise(db_session)

    resp = await app_client.get("/stats/summer-league/teams/lakers")

    assert resp.status_code == 200
    body = resp.text
    assert "Los Angeles Lakers" in body
    assert "2–1" in body  # all-time record
    assert "/stats/summer-league/2025/vegas/" in body  # by-season link


@pytest.mark.asyncio
async def test_franchise_page_unknown_returns_404(app_client: AsyncClient) -> None:
    """An unknown franchise slug 404s rather than rendering an empty page."""
    resp = await app_client.get("/stats/summer-league/teams/not-a-team")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_franchise_with_no_sl_appearances_returns_404(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """A real franchise that has never played Summer League 404s (no entries)."""
    db_session.add(NbaTeam(name="Expansion Team", abbreviation="EXP", slug="expansion"))
    await db_session.commit()

    assert await get_franchise_history(db_session, "expansion") is None
    resp = await app_client.get("/stats/summer-league/teams/expansion")
    assert resp.status_code == 404

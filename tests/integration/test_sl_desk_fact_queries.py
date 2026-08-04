"""Integration tests for the shared ``first_qualifying_games`` lookup (#539).

Focuses on the two new/changed async functions in
``app.services.summer_league.desk_fact_queries``: :func:`fetch_first_qualifying_games`
(the ONE batched ``player_id -> first-qualifying-game`` query the storyline
debut trigger and the fact-path debut status both read) and
:func:`fetch_debut_status` (rewritten to derive from that same lookup).
Pure-function coverage for the underlying reduction lives in
``tests/unit/test_sl_cohort_baselines.py``; ``tests/integration/test_sl_desk_storylines.py``
covers the storyline trigger's own game-scoped firing behavior end to end.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.desk_fact_queries import (
    fetch_debut_status,
    fetch_first_qualifying_games,
)
from tests.integration.perf._capture import count_queries

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


async def _seed_competition(db: AsyncSession, *, year: int) -> SummerLeagueEdition:
    comp = SummerLeagueEdition(
        year=year,
        league_id="15",
        venue_slug=f"las_vegas-{_next_idx()}",
        display_name=f"{year} Las Vegas Summer League",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueEdition
) -> SummerLeagueTeamEntry:
    idx = _next_idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"t-{idx}",
        raw_team_name=f"Team {idx}",
        team_slug=f"team-{idx}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _seed_player(db: AsyncSession, *, name: str) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name,
        last_name="Test",
        display_name=f"{name} Test",
        draft_year=2026,
        draft_round=1,
        draft_pick=1,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _seed_game(
    db: AsyncSession,
    competition: SummerLeagueEdition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    *,
    game_date: date,
) -> SummerLeagueGame:
    idx = _next_idx()
    assert competition.id is not None
    assert home.id is not None
    assert away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"fact-query-game-{idx}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _seed_source_player(
    db: AsyncSession, *, player: PlayerMaster
) -> SummerLeagueSourceRecord:
    idx = _next_idx()
    assert player.id is not None
    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id=f"src-{idx}",
        raw_player_name=player.display_name or "Test Player",
        normalized_name=(player.display_name or "test player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None
    return source_player


async def _seed_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    source_player: SummerLeagueSourceRecord,
    player: PlayerMaster,
    minutes_seconds: int,
    pts: float,
) -> SummerLeaguePlayerGameLog:
    """One box line whose GmSc equals ``pts`` (every other component is 0/None)."""
    idx = _next_idx()
    assert competition.id is not None
    assert game.id is not None
    assert team.id is not None
    assert source_player.id is not None
    assert player.id is not None
    log = SummerLeaguePlayerGameLog(
        competition_id=competition.id,
        game_id=game.id,
        team_entry_id=team.id,
        source_player_id=source_player.id,
        player_id=player.id,
        nba_stats_person_id=f"srcpid-{idx}",
        raw_player_name=player.display_name or "Test Player",
        minutes_seconds=minutes_seconds,
        pts=int(pts),
    )
    db.add(log)
    await db.flush()
    return log


async def test_fetch_first_qualifying_games_picks_earliest_across_years(
    db_session: AsyncSession,
) -> None:
    """Multi-year: a sophomore's first qualifying game predates their return year."""
    comp_2024 = await _seed_competition(db_session, year=2024)
    comp_2025 = await _seed_competition(db_session, year=2025)
    home_24 = await _seed_team(db_session, comp_2024)
    away_24 = await _seed_team(db_session, comp_2024)
    home_25 = await _seed_team(db_session, comp_2025)
    away_25 = await _seed_team(db_session, comp_2025)

    player = await _seed_player(db_session, name="Multi")
    source_24 = await _seed_source_player(db_session, player=player)
    source_25 = await _seed_source_player(db_session, player=player)

    game_2024 = await _seed_game(
        db_session, comp_2024, home_24, away_24, game_date=date(2024, 7, 10)
    )
    await _seed_game_log(
        db_session,
        competition=comp_2024,
        game=game_2024,
        team=home_24,
        source_player=source_24,
        player=player,
        minutes_seconds=20 * 60,
        pts=12.0,
    )
    game_2025 = await _seed_game(
        db_session, comp_2025, home_25, away_25, game_date=date(2025, 7, 10)
    )
    await _seed_game_log(
        db_session,
        competition=comp_2025,
        game=game_2025,
        team=home_25,
        source_player=source_25,
        player=player,
        minutes_seconds=25 * 60,
        pts=22.0,
    )

    assert player.id is not None
    result = await fetch_first_qualifying_games(db_session, player_ids=[player.id])
    assert result[player.id].game_id == game_2024.id
    assert result[player.id].gmsc == 12.0


async def test_fetch_first_qualifying_games_gates_below_floor_multi_game(
    db_session: AsyncSession,
) -> None:
    """A thin (below-floor) first game is skipped for the next qualifying one."""
    comp = await _seed_competition(db_session, year=2026)
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)
    player = await _seed_player(db_session, name="Thin")
    source = await _seed_source_player(db_session, player=player)

    thin_game = await _seed_game(db_session, comp, home, away, game_date=date(2026, 7, 8))
    await _seed_game_log(
        db_session,
        competition=comp,
        game=thin_game,
        team=home,
        source_player=source,
        player=player,
        minutes_seconds=3 * 60,  # below the 10-minute per-game floor
        pts=20.0,
    )
    ok_game = await _seed_game(db_session, comp, home, away, game_date=date(2026, 7, 10))
    await _seed_game_log(
        db_session,
        competition=comp,
        game=ok_game,
        team=home,
        source_player=source,
        player=player,
        minutes_seconds=15 * 60,
        pts=9.0,
    )

    assert player.id is not None
    result = await fetch_first_qualifying_games(db_session, player_ids=[player.id])
    assert result[player.id].game_id == ok_game.id


async def test_fetch_first_qualifying_games_is_one_batched_query_regardless_of_player_count(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Fixed query count (#539): one query whether resolving 1 player or many."""
    comp = await _seed_competition(db_session, year=2026)
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)

    player_ids: list[int] = []
    for i in range(5):
        player = await _seed_player(db_session, name=f"P{i}")
        source = await _seed_source_player(db_session, player=player)
        game = await _seed_game(db_session, comp, home, away, game_date=date(2026, 7, 10))
        await _seed_game_log(
            db_session,
            competition=comp,
            game=game,
            team=home,
            source_player=source,
            player=player,
            minutes_seconds=20 * 60,
            pts=10.0,
        )
        assert player.id is not None
        player_ids.append(player.id)

    with count_queries(async_engine) as captured:
        result = await fetch_first_qualifying_games(db_session, player_ids=player_ids)

    assert len(result) == 5
    assert len(captured) == 1


async def test_fetch_debut_status_reuses_the_shared_first_qualifying_lookup(
    db_session: AsyncSession,
) -> None:
    """fetch_debut_status (#539) agrees with fetch_first_qualifying_games's verdict."""
    comp_2024 = await _seed_competition(db_session, year=2024)
    home = await _seed_team(db_session, comp_2024)
    away = await _seed_team(db_session, comp_2024)

    veteran = await _seed_player(db_session, name="Veteran")
    veteran_source = await _seed_source_player(db_session, player=veteran)
    veteran_game = await _seed_game(
        db_session, comp_2024, home, away, game_date=date(2024, 7, 10)
    )
    await _seed_game_log(
        db_session,
        competition=comp_2024,
        game=veteran_game,
        team=home,
        source_player=veteran_source,
        player=veteran,
        minutes_seconds=20 * 60,
        pts=10.0,
    )

    rookie = await _seed_player(db_session, name="Rookie")

    assert veteran.id is not None
    assert rookie.id is not None
    status = await fetch_debut_status(
        db_session, player_ids=[veteran.id, rookie.id], before_year=2026
    )
    assert status[veteran.id] is False
    assert status[rookie.id] is True

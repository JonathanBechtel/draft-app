"""Integration tests for assisted-FG columns on SummerLeaguePlayerSeason.

Exercises metrics.rebuild() end-to-end against a real Postgres test schema and
asserts that:

1. ast_fgm / unast_fgm are populated after rebuild() when
   SummerLeaguePlayByPlayEvent rows exist for the player-competition.
2. A player with NO PBP made-FG data gets NULL ast_fgm / unast_fgm.
3. Events with person1_id=NULL (unresolved scorer) are excluded.
4. The player route's shotchart context exposes assisted_fg_pct for PBP-era
   players and None for players without PBP data.

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeagueResolutionStatus,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.metrics import rebuild
from app.services.summer_league_stats_service import get_player_shotchart_context


# ---------------------------------------------------------------------------
# Seed helpers (mirror test_sl_metrics_shotdiet.py conventions)
# ---------------------------------------------------------------------------


async def _make_player(db: AsyncSession, *, slug: str) -> PlayerMaster:
    p = PlayerMaster(display_name=slug.replace("-", " ").title(), slug=slug)
    db.add(p)
    await db.flush()
    assert p.id is not None
    return p


async def _make_competition(
    db: AsyncSession, *, year: int = 2024, league_id: str = "15"
) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"las_vegas_{year}_{league_id}",
        display_name=f"Las Vegas {year}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 14),
        data_quality=SummerLeagueDataQuality.RAW_ONLY,
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _make_team_entry(
    db: AsyncSession, *, competition_id: int, nba_stats_team_id: str
) -> SummerLeagueTeamEntry:
    entry = SummerLeagueTeamEntry(
        competition_id=competition_id,
        nba_stats_team_id=nba_stats_team_id,
        raw_team_name=f"Team {nba_stats_team_id}",
        team_slug=f"team-{nba_stats_team_id}-{competition_id}",
    )
    db.add(entry)
    await db.flush()
    assert entry.id is not None
    return entry


async def _make_game(
    db: AsyncSession, *, competition_id: int, nba_stats_game_id: str
) -> SummerLeagueGame:
    game = SummerLeagueGame(
        competition_id=competition_id,
        nba_stats_game_id=nba_stats_game_id,
        home_team_nba_stats_id="1610612753",
        away_team_nba_stats_id="1610612739",
        game_date=date(2024, 7, 12),
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _make_source_player(
    db: AsyncSession,
    *,
    nba_stats_person_id: str,
    canonical_player_id: int | None = None,
) -> SummerLeagueSourcePlayer:
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=nba_stats_person_id,
        raw_player_name=f"Player {nba_stats_person_id}",
        normalized_name=f"player{nba_stats_person_id}",
        first_seen_year=2024,
        last_seen_year=2024,
        canonical_player_id=canonical_player_id,
        resolution_status=(
            SummerLeagueResolutionStatus.EXTERNAL_ID
            if canonical_player_id
            else SummerLeagueResolutionStatus.UNRESOLVED
        ),
        resolution_confidence=1.0 if canonical_player_id else 0.0,
        resolved_by="test" if canonical_player_id else None,
    )
    db.add(sp)
    await db.flush()
    assert sp.id is not None
    return sp


def _player_game_log(
    *,
    competition_id: int,
    player_id: int,
    source_player_id: int,
    team_entry_id: int,
    game_id: int,
    nba_stats_person_id: str = "9991",
    fgm: int = 5,
    fga: int = 10,
    fg3m: int = 1,
    fg3a: int = 3,
    pts: int = 12,
    minutes_seconds: int = 1800,
) -> SummerLeaguePlayerGameLog:
    return SummerLeaguePlayerGameLog(
        competition_id=competition_id,
        player_id=player_id,
        source_player_id=source_player_id,
        team_entry_id=team_entry_id,
        game_id=game_id,
        nba_stats_person_id=nba_stats_person_id,
        raw_player_name=f"Player {nba_stats_person_id}",
        minutes_seconds=minutes_seconds,
        fgm=fgm,
        fga=fga,
        fg3m=fg3m,
        fg3a=fg3a,
        ftm=2,
        fta=2,
        oreb=1,
        dreb=3,
        reb=4,
        ast=2,
        stl=1,
        blk=0,
        tov=1,
        pf=2,
        pts=pts,
        plus_minus=5,
    )


def _team_game_log(
    *,
    competition_id: int,
    team_entry_id: int,
    game_id: int,
    minutes: int = 200,
) -> SummerLeagueTeamGameLog:
    return SummerLeagueTeamGameLog(
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        game_id=game_id,
        minutes=minutes,
        fgm=30,
        fga=70,
        fg3m=10,
        fg3a=25,
        ftm=15,
        fta=20,
        oreb=10,
        dreb=30,
        reb=40,
        ast=20,
        stl=8,
        blk=4,
        tov=12,
        pf=20,
        pts=85,
        plus_minus=6,
    )


def _pbp_event(
    *,
    game_id: int,
    competition_id: int,
    nba_stats_game_id: str,
    event_num: int,
    scorer_id: int | None,
    assister_id: int | None = None,
    event_msg_type: int = 1,
) -> SummerLeaguePlayByPlayEvent:
    """Create a PBP event row directly (bypasses normalization pipeline).

    Args:
        game_id: FK to summer_league_games.
        competition_id: FK to summer_league_competitions.
        nba_stats_game_id: Raw NBA game identifier string.
        event_num: Unique event sequence number within the game.
        scorer_id: Canonical player FK for person1 (the scorer).
        assister_id: Canonical player FK for person2 (the assister, or None).
        event_msg_type: NBA event type; 1 = made field goal.
    """
    return SummerLeaguePlayByPlayEvent(
        game_id=game_id,
        competition_id=competition_id,
        nba_stats_game_id=nba_stats_game_id,
        event_num=event_num,
        event_msg_type=event_msg_type,
        period=1,
        clock="10:00",
        person1_id=scorer_id,
        person2_id=assister_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assisted_fg_columns_populate_after_rebuild(
    db_session: AsyncSession,
) -> None:
    """rebuild() populates ast_fgm / unast_fgm when PBP made-FG events exist.

    Seeds 7 made-FG events: 4 assisted (person2_id set), 3 unassisted
    (person2_id NULL).

    Expected:
        ast_fgm  = 4
        unast_fgm = 3
    """
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612753"
    )
    assert team.id is not None
    opp_team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612739"
    )
    assert opp_team.id is not None
    game = await _make_game(
        db_session, competition_id=comp.id, nba_stats_game_id="1522400201"
    )
    assert game.id is not None

    scorer = await _make_player(db_session, slug="assisted-scorer")
    assert scorer.id is not None
    assister = await _make_player(db_session, slug="the-assister")
    assert assister.id is not None
    sp_scorer = await _make_source_player(
        db_session, nba_stats_person_id="8881", canonical_player_id=scorer.id
    )
    assert sp_scorer.id is not None

    # Box logs so scorer appears in the metrics computation.
    db_session.add(
        _player_game_log(
            competition_id=comp.id,
            player_id=scorer.id,
            source_player_id=sp_scorer.id,
            team_entry_id=team.id,
            game_id=game.id,
            nba_stats_person_id="8881",
            fgm=7,
            fga=14,
        )
    )
    db_session.add(
        _team_game_log(competition_id=comp.id, team_entry_id=team.id, game_id=game.id)
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )

    # 4 assisted made-FGs (person2_id = assister.id).
    for i in range(4):
        db_session.add(
            _pbp_event(
                game_id=game.id,
                competition_id=comp.id,
                nba_stats_game_id="1522400201",
                event_num=i + 1,
                scorer_id=scorer.id,
                assister_id=assister.id,
            )
        )
    # 3 unassisted made-FGs (person2_id = None).
    for i in range(3):
        db_session.add(
            _pbp_event(
                game_id=game.id,
                competition_id=comp.id,
                nba_stats_game_id="1522400201",
                event_num=10 + i + 1,
                scorer_id=scorer.id,
                assister_id=None,
            )
        )

    await db_session.flush()

    await rebuild(db_session)
    await db_session.flush()

    season = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.player_id == scorer.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert season.ast_fgm == 4
    assert season.unast_fgm == 3


@pytest.mark.asyncio
async def test_assisted_fg_null_when_no_pbp_data(db_session: AsyncSession) -> None:
    """A player with NO PBP events gets NULL ast_fgm / unast_fgm.

    Box stats still populate; only the PBP-derived columns stay NULL.
    """
    comp = await _make_competition(db_session, year=2023, league_id="15")
    assert comp.id is not None
    team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612753"
    )
    assert team.id is not None
    opp_team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612739"
    )
    assert opp_team.id is not None
    game = await _make_game(
        db_session, competition_id=comp.id, nba_stats_game_id="1521300201"
    )
    assert game.id is not None
    player = await _make_player(db_session, slug="no-pbp-player")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="8882", canonical_player_id=player.id
    )
    assert sp.id is not None

    db_session.add(
        _player_game_log(
            competition_id=comp.id,
            player_id=player.id,
            source_player_id=sp.id,
            team_entry_id=team.id,
            game_id=game.id,
            nba_stats_person_id="8882",
        )
    )
    db_session.add(
        _team_game_log(competition_id=comp.id, team_entry_id=team.id, game_id=game.id)
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )
    # No PBP events added.
    await db_session.flush()

    await rebuild(db_session)
    await db_session.flush()

    season = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.player_id == player.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert season.ast_fgm is None
    assert season.unast_fgm is None
    # Box stats still populated.
    assert season.fga > 0


@pytest.mark.asyncio
async def test_assisted_fg_excludes_unresolved_scorer_events(
    db_session: AsyncSession,
) -> None:
    """PBP made-FG events with person1_id=NULL are excluded from counts.

    Seeds 2 PBP events: 1 with a resolved scorer, 1 with person1_id=NULL.
    Only the resolved event should count.

    Expected: ast_fgm=1, unast_fgm=0 (the unresolved event is invisible).
    """
    comp = await _make_competition(db_session, year=2024, league_id="14")
    assert comp.id is not None
    team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612753"
    )
    assert team.id is not None
    opp_team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612739"
    )
    assert opp_team.id is not None
    game = await _make_game(
        db_session, competition_id=comp.id, nba_stats_game_id="1522400202"
    )
    assert game.id is not None
    scorer = await _make_player(db_session, slug="partial-scorer")
    assert scorer.id is not None
    assister = await _make_player(db_session, slug="partial-assister")
    assert assister.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="8883", canonical_player_id=scorer.id
    )
    assert sp.id is not None

    db_session.add(
        _player_game_log(
            competition_id=comp.id,
            player_id=scorer.id,
            source_player_id=sp.id,
            team_entry_id=team.id,
            game_id=game.id,
            nba_stats_person_id="8883",
        )
    )
    db_session.add(
        _team_game_log(competition_id=comp.id, team_entry_id=team.id, game_id=game.id)
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )

    # 1 assisted made-FG with resolved scorer.
    db_session.add(
        _pbp_event(
            game_id=game.id,
            competition_id=comp.id,
            nba_stats_game_id="1522400202",
            event_num=1,
            scorer_id=scorer.id,
            assister_id=assister.id,
        )
    )
    # 1 made-FG with no resolved scorer (person1_id = NULL); should be excluded.
    db_session.add(
        _pbp_event(
            game_id=game.id,
            competition_id=comp.id,
            nba_stats_game_id="1522400202",
            event_num=2,
            scorer_id=None,
            assister_id=None,
        )
    )
    await db_session.flush()

    await rebuild(db_session)
    await db_session.flush()

    season = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.player_id == scorer.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert season.ast_fgm == 1
    assert season.unast_fgm == 0


@pytest.mark.asyncio
async def test_shotchart_context_exposes_assisted_fg_pct(
    db_session: AsyncSession,
) -> None:
    """get_player_shotchart_context returns assisted_fg_pct for PBP-era players.

    Seeds a player with shot events (so sl_shotchart is not None) and PBP made-FG
    events (4 assisted, 6 unassisted).  Confirms assisted_fg_pct = 4/10 = 0.4 in
    the shotchart context dict.

    Also seeds a second player with shot data but no PBP events and confirms
    assisted_fg_pct is None for them.
    """
    from app.schemas.summer_league import SummerLeagueShotEvent

    comp = await _make_competition(db_session, year=2024, league_id="16")
    assert comp.id is not None
    team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612753"
    )
    assert team.id is not None
    opp_team = await _make_team_entry(
        db_session, competition_id=comp.id, nba_stats_team_id="1610612739"
    )
    assert opp_team.id is not None
    game = await _make_game(
        db_session, competition_id=comp.id, nba_stats_game_id="1522400210"
    )
    assert game.id is not None

    # Player A: has PBP data.
    player_a = await _make_player(db_session, slug="pbp-player-a")
    assert player_a.id is not None
    sp_a = await _make_source_player(
        db_session, nba_stats_person_id="8884", canonical_player_id=player_a.id
    )
    assert sp_a.id is not None

    # Player B: no PBP data.
    player_b = await _make_player(db_session, slug="no-pbp-player-b")
    assert player_b.id is not None
    sp_b = await _make_source_player(
        db_session, nba_stats_person_id="8885", canonical_player_id=player_b.id
    )
    assert sp_b.id is not None

    for sp, player in [(sp_a, player_a), (sp_b, player_b)]:
        assert player.id is not None
        assert sp.id is not None
        db_session.add(
            _player_game_log(
                competition_id=comp.id,
                player_id=player.id,
                source_player_id=sp.id,
                team_entry_id=team.id,
                game_id=game.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                fgm=10,
                fga=20,
            )
        )
        # Add some shot events so the shotchart context is non-None.
        for ev_i in range(20):
            db_session.add(
                SummerLeagueShotEvent(
                    game_id=game.id,
                    competition_id=comp.id,
                    team_entry_id=team.id,
                    source_player_id=sp.id,
                    player_id=player.id,
                    nba_stats_person_id=sp.nba_stats_person_id,
                    nba_stats_game_id="1522400210",
                    nba_stats_game_event_id=ev_i + 1 + (1000 if player is player_b else 0),
                    shot_zone_basic="Restricted Area",
                    shot_zone_area="Center(C)",
                    shot_zone_range="Less Than 8 ft.",
                    shot_type="2PT Field Goal",
                    loc_x=0,
                    loc_y=50,
                    shot_distance=5,
                    made=True,
                    period=1,
                    minutes_remaining=9,
                    seconds_remaining=0,
                )
            )
    db_session.add(
        _team_game_log(competition_id=comp.id, team_entry_id=team.id, game_id=game.id)
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )

    assister = await _make_player(db_session, slug="passer-ctx")
    assert assister.id is not None

    # 4 assisted + 6 unassisted PBP made-FG events for player_a only.
    for i in range(4):
        db_session.add(
            _pbp_event(
                game_id=game.id,
                competition_id=comp.id,
                nba_stats_game_id="1522400210",
                event_num=100 + i,
                scorer_id=player_a.id,
                assister_id=assister.id,
            )
        )
    for i in range(6):
        db_session.add(
            _pbp_event(
                game_id=game.id,
                competition_id=comp.id,
                nba_stats_game_id="1522400210",
                event_num=200 + i,
                scorer_id=player_a.id,
                assister_id=None,
            )
        )
    await db_session.flush()

    await rebuild(db_session)
    await db_session.flush()

    ctx_a = await get_player_shotchart_context(db_session, player_a.id)
    assert ctx_a is not None
    assert ctx_a["shot_diet"] is not None
    assert ctx_a["shot_diet"]["assisted_fg_pct"] == pytest.approx(0.4, abs=1e-4)

    ctx_b = await get_player_shotchart_context(db_session, player_b.id)
    assert ctx_b is not None
    # Player B has shot data but no PBP events → assisted_fg_pct should be None.
    diet_b = ctx_b.get("shot_diet")
    if diet_b is not None:
        assert diet_b.get("assisted_fg_pct") is None

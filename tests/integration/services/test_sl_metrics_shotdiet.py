"""Integration tests for shot-diet columns on SummerLeaguePlayerSeason.

Exercises the metrics.rebuild() pipeline end-to-end against a real Postgres
test schema and asserts that:

1. rim_rate / mid_rate / three_rate / corner3_rate are populated after rebuild
   when SummerLeagueShotEvent rows exist for the player-competition.
2. A player whose shots span two same-year venues has shot counts summed per
   competition (each row gets its own competition's shot data, not merged).
3. A player with NO shot data for their competition gets NULL rates.
4. corner3_rate is correctly a sub-fraction of three_rate.

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
    SummerLeaguePlayerGameLog,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.metrics import rebuild


# ---------------------------------------------------------------------------
# Seed helpers
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


def _shot_event(
    *,
    game_id: int,
    competition_id: int,
    team_entry_id: int,
    source_player_id: int,
    player_id: int,
    nba_stats_game_id: str,
    nba_stats_person_id: str = "9991",
    event_id: int,
    zone: str,
    made: bool = True,
) -> SummerLeagueShotEvent:
    return SummerLeagueShotEvent(
        game_id=game_id,
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        player_id=player_id,
        nba_stats_person_id=nba_stats_person_id,
        nba_stats_game_id=nba_stats_game_id,
        nba_stats_game_event_id=event_id,
        shot_zone_basic=zone,
        shot_zone_area="Center(C)",
        shot_zone_range="Less Than 8 ft.",
        shot_type="2PT Field Goal",
        loc_x=0,
        loc_y=50,
        shot_distance=5,
        made=made,
        period=1,
        minutes_remaining=9,
        seconds_remaining=0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shot_diet_columns_populate_after_rebuild(db_session: AsyncSession) -> None:
    """rebuild() populates rim/mid/three/corner3 rates when shot data exists.

    Seeds:
    - 10 Restricted Area shots → rim_rate = 10/20 = 0.5
    - 4 Mid-Range shots       → mid_rate  =  4/20 = 0.2
    - 4 Above the Break 3     → three_rate = 6/20 = 0.3
    - 2 Left Corner 3         → corner3_rate = 2/20 = 0.1
    Total 20 non-backcourt shots.
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
        db_session, competition_id=comp.id, nba_stats_game_id="1522400099"
    )
    assert game.id is not None
    player = await _make_player(db_session, slug="rim-rusher")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="9991", canonical_player_id=player.id
    )
    assert sp.id is not None

    # Box logs so the player appears in the metrics computation.
    db_session.add(
        _player_game_log(
            competition_id=comp.id,
            player_id=player.id,
            source_player_id=sp.id,
            team_entry_id=team.id,
            game_id=game.id,
            nba_stats_person_id="9991",
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=team.id, game_id=game.id
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )

    # Shot events: 10 RA, 4 Mid-Range, 4 AB3, 2 Corner 3L.
    zones = (
        [("Restricted Area", True)] * 10
        + [("Mid-Range", False)] * 4
        + [("Above the Break 3", True)] * 4
        + [("Left Corner 3", False)] * 2
    )
    for i, (zone, made) in enumerate(zones):
        db_session.add(
            _shot_event(
                game_id=game.id,
                competition_id=comp.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_game_id="1522400099",
                event_id=i + 1,
                zone=zone,
                made=made,
            )
        )
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

    assert season.rim_rate == pytest.approx(10 / 20, abs=1e-4)
    assert season.mid_rate == pytest.approx(4 / 20, abs=1e-4)
    assert season.three_rate == pytest.approx(6 / 20, abs=1e-4)
    assert season.corner3_rate == pytest.approx(2 / 20, abs=1e-4)
    # corner3 must be a sub-fraction of three (LC3 ⊂ 3-point zones).
    assert season.corner3_rate <= season.three_rate  # type: ignore[operator]


@pytest.mark.asyncio
async def test_shot_diet_null_when_no_shot_data(db_session: AsyncSession) -> None:
    """A player with NO SummerLeagueShotEvent rows gets NULL shot-diet rates.

    The metrics rebuild still runs and populates box/efficiency columns; only
    the shot-diet columns stay NULL because no shot-chart data exists.
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
        db_session, competition_id=comp.id, nba_stats_game_id="1521300099"
    )
    assert game.id is not None
    player = await _make_player(db_session, slug="no-shot-data")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="9992", canonical_player_id=player.id
    )
    assert sp.id is not None
    db_session.add(
        _player_game_log(
            competition_id=comp.id,
            player_id=player.id,
            source_player_id=sp.id,
            team_entry_id=team.id,
            game_id=game.id,
            nba_stats_person_id="9992",
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=team.id, game_id=game.id
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )
    # No shot events added.
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

    assert season.rim_rate is None
    assert season.mid_rate is None
    assert season.three_rate is None
    assert season.corner3_rate is None
    # Box stats still populated.
    assert season.fga > 0


@pytest.mark.asyncio
async def test_shot_diet_per_competition_not_merged(db_session: AsyncSession) -> None:
    """A player with shots in two competitions gets separate diet rows per comp.

    Comp A: 8 RA + 2 Above-Break-3 = 10 shots → rim_rate=0.8, three_rate=0.2
    Comp B: 3 RA + 7 Mid-Range = 10 shots     → rim_rate=0.3, mid_rate=0.7
    The rebuild must NOT merge the two; each SummerLeaguePlayerSeason row gets
    its own competition's shot diet.
    """
    comp_a = await _make_competition(db_session, year=2024, league_id="15")
    comp_b = await _make_competition(db_session, year=2024, league_id="14")
    assert comp_a.id is not None
    assert comp_b.id is not None

    team_a = await _make_team_entry(
        db_session, competition_id=comp_a.id, nba_stats_team_id="1610612753"
    )
    assert team_a.id is not None
    opp_a = await _make_team_entry(
        db_session, competition_id=comp_a.id, nba_stats_team_id="1610612739"
    )
    assert opp_a.id is not None
    team_b = await _make_team_entry(
        db_session, competition_id=comp_b.id, nba_stats_team_id="1610612753"
    )
    assert team_b.id is not None
    opp_b = await _make_team_entry(
        db_session, competition_id=comp_b.id, nba_stats_team_id="1610612739"
    )
    assert opp_b.id is not None

    game_a = await _make_game(
        db_session, competition_id=comp_a.id, nba_stats_game_id="1522400201"
    )
    assert game_a.id is not None
    game_b = await _make_game(
        db_session, competition_id=comp_b.id, nba_stats_game_id="1521400201"
    )
    assert game_b.id is not None

    player = await _make_player(db_session, slug="two-venue-player")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="9993", canonical_player_id=player.id
    )
    assert sp.id is not None

    # Box logs for comp_a.
    db_session.add(
        _player_game_log(
            competition_id=comp_a.id,
            player_id=player.id,
            source_player_id=sp.id,
            team_entry_id=team_a.id,
            game_id=game_a.id,
            nba_stats_person_id="9993",
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp_a.id, team_entry_id=team_a.id, game_id=game_a.id
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp_a.id, team_entry_id=opp_a.id, game_id=game_a.id
        )
    )

    # Box logs for comp_b.
    db_session.add(
        _player_game_log(
            competition_id=comp_b.id,
            player_id=player.id,
            source_player_id=sp.id,
            team_entry_id=team_b.id,
            game_id=game_b.id,
            nba_stats_person_id="9993",
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp_b.id, team_entry_id=team_b.id, game_id=game_b.id
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp_b.id, team_entry_id=opp_b.id, game_id=game_b.id
        )
    )

    # Comp A: 8 RA + 2 AB3 = 10 shots.
    zones_a = [("Restricted Area", True)] * 8 + [("Above the Break 3", False)] * 2
    for i, (zone, made) in enumerate(zones_a):
        db_session.add(
            _shot_event(
                game_id=game_a.id,
                competition_id=comp_a.id,
                team_entry_id=team_a.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_game_id="1522400201",
                event_id=i + 1,
                zone=zone,
                made=made,
            )
        )

    # Comp B: 3 RA + 7 Mid-Range = 10 shots.
    zones_b = [("Restricted Area", True)] * 3 + [("Mid-Range", False)] * 7
    for i, (zone, made) in enumerate(zones_b):
        db_session.add(
            _shot_event(
                game_id=game_b.id,
                competition_id=comp_b.id,
                team_entry_id=team_b.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_game_id="1521400201",
                event_id=i + 1,
                zone=zone,
                made=made,
            )
        )

    await db_session.flush()
    await rebuild(db_session)
    await db_session.flush()

    seasons = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.player_id == player.id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()

    assert len(seasons) == 2
    by_comp = {s.competition_id: s for s in seasons}

    row_a = by_comp[comp_a.id]
    assert row_a.rim_rate == pytest.approx(8 / 10, abs=1e-4)
    assert row_a.three_rate == pytest.approx(2 / 10, abs=1e-4)
    assert row_a.mid_rate == pytest.approx(0.0, abs=1e-4)

    row_b = by_comp[comp_b.id]
    assert row_b.rim_rate == pytest.approx(3 / 10, abs=1e-4)
    assert row_b.mid_rate == pytest.approx(7 / 10, abs=1e-4)
    assert row_b.three_rate == pytest.approx(0.0, abs=1e-4)


@pytest.mark.asyncio
async def test_backcourt_shots_excluded_from_diet(db_session: AsyncSession) -> None:
    """Backcourt shots are excluded from diet totals and denominator.

    Seeds: 5 Restricted Area + 2 Backcourt.
    Denominator must be 5 (not 7); rim_rate = 1.0.
    """
    comp = await _make_competition(db_session, year=2022, league_id="15")
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
        db_session, competition_id=comp.id, nba_stats_game_id="1520200099"
    )
    assert game.id is not None
    player = await _make_player(db_session, slug="backcourt-heaver")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="9994", canonical_player_id=player.id
    )
    assert sp.id is not None

    db_session.add(
        _player_game_log(
            competition_id=comp.id,
            player_id=player.id,
            source_player_id=sp.id,
            team_entry_id=team.id,
            game_id=game.id,
            nba_stats_person_id="9994",
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=team.id, game_id=game.id
        )
    )
    db_session.add(
        _team_game_log(
            competition_id=comp.id, team_entry_id=opp_team.id, game_id=game.id
        )
    )

    zones = [("Restricted Area", True)] * 5 + [("Backcourt", False)] * 2
    for i, (zone, made) in enumerate(zones):
        # Backcourt shot zone_basic = "Backcourt" — the query excludes it.
        db_session.add(
            _shot_event(
                game_id=game.id,
                competition_id=comp.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_game_id="1520200099",
                event_id=i + 1,
                zone=zone,
                made=made,
            )
        )
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

    # 5 RA / 5 non-backcourt = 1.0; backcourt excluded from denominator.
    assert season.rim_rate == pytest.approx(1.0, abs=1e-4)
    assert season.mid_rate == pytest.approx(0.0, abs=1e-4)
    assert season.three_rate == pytest.approx(0.0, abs=1e-4)

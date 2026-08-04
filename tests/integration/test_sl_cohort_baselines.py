"""Integration tests for the Summer League Desk cohort-baseline builder (#502).

Seeds `SummerLeagueEdition` + `SummerLeaguePlayerSeason` history plus draft
slots on `PlayerMaster`, runs `build_baselines`, and asserts the persisted T1
distributions and version-flip behavior end to end.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.cohort_baselines import build_baselines

pytestmark = pytest.mark.asyncio


async def _seed_competition(
    db: AsyncSession, *, year: int, venue_slug: str, league_id: str
) -> SummerLeagueEdition:
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
    return comp


async def _seed_player(
    db: AsyncSession,
    *,
    name: str,
    draft_round: int | None,
    draft_pick: int | None,
) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name,
        last_name="Test",
        display_name=f"{name} Test",
        draft_year=2020,
        draft_round=draft_round,
        draft_pick=draft_pick,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _seed_season(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    player: PlayerMaster,
    year: int,
    gmsc: float,
    minutes: float,
    gp: int,
) -> SummerLeaguePlayerSeason:
    assert competition.id is not None
    assert player.id is not None
    season = SummerLeaguePlayerSeason(
        competition_id=competition.id,
        player_id=player.id,
        year=year,
        venue_slug=competition.venue_slug,
        is_current=True,
        gp=gp,
        minutes=minutes,
        gmsc=gmsc,
    )
    db.add(season)
    await db.flush()
    return season


_GAME_IDX = {"i": 0}


def _next_game_idx() -> int:
    _GAME_IDX["i"] += 1
    return _GAME_IDX["i"]


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueEdition
) -> SummerLeagueTeamEntry:
    idx = _next_game_idx()
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


async def _seed_game(
    db: AsyncSession,
    competition: SummerLeagueEdition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
) -> SummerLeagueGame:
    idx = _next_game_idx()
    assert competition.id is not None
    assert home.id is not None
    assert away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"cohort-baseline-game-{idx}",
        game_date=date(competition.year, 7, 10),
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
    idx = _next_game_idx()
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
    idx = _next_game_idx()
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


async def _active_rows(
    db: AsyncSession, version: str
) -> dict[str, SummerLeagueCohortBaseline]:
    stmt = select(SummerLeagueCohortBaseline).where(
        SummerLeagueCohortBaseline.baseline_version == version  # type: ignore[arg-type]
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {r.cohort_key: r for r in rows}


async def test_build_baselines_lottery_window_and_stats(
    db_session: AsyncSession,
) -> None:
    """Pick #1's ±3 window (clamped to 1-14) is its own slot:1-4 cohort.

    Each lottery pick centers its own window (pick 2's is 1-5, not 1-4), so
    the cohort a subject is ranked against is built from OTHER players who
    were also picked #1 across the history window -- not from picks 1-4
    collectively.
    """
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )

    # Four different #1 picks (distinct player identities) all land in the
    # same slot:1-4 cohort.
    scores = [10.0, 20.0, 30.0, 40.0]
    for i, gmsc in enumerate(scores):
        player = await _seed_player(
            db_session, name=f"Pick1_{i}", draft_round=1, draft_pick=1
        )
        await _seed_season(
            db_session,
            competition=comp,
            player=player,
            year=2024,
            gmsc=gmsc,
            minutes=100.0,
            gp=5,
        )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()

    rows = await _active_rows(db_session, version)
    slot_1_4 = rows["slot:1-4"]
    assert slot_1_4.cohort_kind == SummerLeagueDeskCohortKind.SLOT_WINDOW
    assert slot_1_4.grain == SummerLeagueDeskGrain.EVENT
    assert slot_1_4.slot_low == 1
    assert slot_1_4.slot_high == 4
    assert slot_1_4.n_members == 4
    assert slot_1_4.mean_value == 25.0
    assert slot_1_4.median_value == 25.0
    assert slot_1_4.breakpoints["50"] == 25.0
    assert slot_1_4.is_active is True


async def test_build_baselines_round_bucket_and_undrafted_and_debut(
    db_session: AsyncSession,
) -> None:
    """R1-late, R2, undrafted cohorts populate correctly, plus a debut-grain row."""
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)

    r1_late = await _seed_player(
        db_session, name="R1Late", draft_round=1, draft_pick=20
    )
    await _seed_season(
        db_session,
        competition=comp,
        player=r1_late,
        year=2024,
        gmsc=15.0,
        minutes=100.0,
        gp=5,
    )
    # Debut grain (#539) is built from the player's earliest qualifying
    # individual GAME, not the season aggregate -- seed one qualifying game
    # log whose GmSc (== pts, per `_seed_game_log`) matches the season value
    # so the debut assertion below stays hand-computable.
    r1_late_source = await _seed_source_player(db_session, player=r1_late)
    r1_late_game = await _seed_game(db_session, comp, home, away)
    await _seed_game_log(
        db_session,
        competition=comp,
        game=r1_late_game,
        team=home,
        source_player=r1_late_source,
        player=r1_late,
        minutes_seconds=25 * 60,
        pts=15.0,
    )

    # R2's within-round pick is 1-30 (overall picks 31-60) -- NOT pick<=14.
    r2 = await _seed_player(db_session, name="R2", draft_round=2, draft_pick=5)
    await _seed_season(
        db_session,
        competition=comp,
        player=r2,
        year=2024,
        gmsc=5.0,
        minutes=100.0,
        gp=5,
    )

    undrafted = await _seed_player(
        db_session, name="Undrafted", draft_round=None, draft_pick=None
    )
    await _seed_season(
        db_session,
        competition=comp,
        player=undrafted,
        year=2024,
        gmsc=8.0,
        minutes=100.0,
        gp=5,
    )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    r1_late_row = rows["round:1_late"]
    assert r1_late_row.cohort_kind == SummerLeagueDeskCohortKind.ROUND_BUCKET
    assert (r1_late_row.slot_low, r1_late_row.slot_high) == (15, 30)
    assert r1_late_row.n_members == 1
    assert r1_late_row.mean_value == 15.0

    r2_row = rows["round:2"]
    assert r2_row.cohort_kind == SummerLeagueDeskCohortKind.ROUND_BUCKET
    # Round 2's within-round 1-30 window == overall picks 31-60.
    assert (r2_row.slot_low, r2_row.slot_high) == (31, 60)
    assert r2_row.mean_value == 5.0

    undrafted_row = rows["status:undrafted"]
    assert undrafted_row.cohort_kind == SummerLeagueDeskCohortKind.STATUS
    assert undrafted_row.slot_low is None
    assert undrafted_row.slot_high is None
    assert undrafted_row.mean_value == 8.0

    # Every player's only year is also their debut year.
    debut_row = rows["debut:1_late"]
    assert debut_row.cohort_kind == SummerLeagueDeskCohortKind.DEBUT
    assert debut_row.grain == SummerLeagueDeskGrain.DEBUT
    assert debut_row.n_members == 1
    assert debut_row.mean_value == 15.0


async def test_build_baselines_debut_grain_uses_earliest_year_only(
    db_session: AsyncSession,
) -> None:
    """A sophomore's return year feeds the event cohort but not the debut cohort."""
    comp_2023 = await _seed_competition(
        db_session, year=2023, venue_slug="las_vegas", league_id="13"
    )
    comp_2024 = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )

    player = await _seed_player(
        db_session, name="Sophomore", draft_round=1, draft_pick=1
    )
    await _seed_season(
        db_session,
        competition=comp_2023,
        player=player,
        year=2023,
        gmsc=10.0,
        minutes=100.0,
        gp=5,
    )
    await _seed_season(
        db_session,
        competition=comp_2024,
        player=player,
        year=2024,
        gmsc=30.0,
        minutes=100.0,
        gp=5,
    )

    # Debut grain (#539) is game-based: seed one qualifying game log per year
    # (each competition's `_seed_game` date carries its own year, so these
    # sort chronologically) so the 2023 game -- not the 2024 one -- wins.
    source_player = await _seed_source_player(db_session, player=player)
    home_2023 = await _seed_team(db_session, comp_2023)
    away_2023 = await _seed_team(db_session, comp_2023)
    game_2023 = await _seed_game(db_session, comp_2023, home_2023, away_2023)
    await _seed_game_log(
        db_session,
        competition=comp_2023,
        game=game_2023,
        team=home_2023,
        source_player=source_player,
        player=player,
        minutes_seconds=25 * 60,
        pts=10.0,
    )
    home_2024 = await _seed_team(db_session, comp_2024)
    away_2024 = await _seed_team(db_session, comp_2024)
    game_2024 = await _seed_game(db_session, comp_2024, home_2024, away_2024)
    await _seed_game_log(
        db_session,
        competition=comp_2024,
        game=game_2024,
        team=home_2024,
        source_player=source_player,
        player=player,
        minutes_seconds=25 * 60,
        pts=30.0,
    )

    version = await build_baselines(
        db_session, season_range="2023-2024", min_minutes=40.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    # Event grain: both years count (2 events).
    event_row = rows["slot:1-4"]
    assert event_row.n_members == 2
    assert event_row.mean_value == 20.0  # (10 + 30) / 2

    # Debut grain: only the earliest year (2023, GmSc 10.0) counts.
    debut_row = rows["debut:1-4"]
    assert debut_row.n_members == 1
    assert debut_row.mean_value == 10.0


async def test_build_baselines_min_minutes_gate_excludes_thin_samples(
    db_session: AsyncSession,
) -> None:
    """A player-year under the minutes floor never enters the distribution."""
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )
    thin = await _seed_player(db_session, name="Thin", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=comp,
        player=thin,
        year=2024,
        gmsc=99.0,
        minutes=5.0,
        gp=1,
    )
    qualified = await _seed_player(
        db_session, name="Qualified", draft_round=1, draft_pick=2
    )
    await _seed_season(
        db_session,
        competition=comp,
        player=qualified,
        year=2024,
        gmsc=11.0,
        minutes=50.0,
        gp=5,
    )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    # The thin sample (pick 1, 5 minutes) never enters ANY cohort -- the gate
    # drops it before a cohort_key is even assigned. Only pick 2's own
    # slot:1-5 window shows up, with just the qualified member.
    assert rows["slot:1-5"].n_members == 1
    assert rows["slot:1-5"].mean_value == 11.0


async def test_build_baselines_no_qualifying_events_raises(
    db_session: AsyncSession,
) -> None:
    """An empty history refuses to write an empty baseline_version."""
    with pytest.raises(ValueError):
        await build_baselines(db_session, season_range="1999-1999", min_minutes=40.0)


async def test_build_baselines_idempotent_rerun_flips_active_without_corrupting(
    db_session: AsyncSession,
) -> None:
    """Re-running writes a new version and deactivates the old one intact."""
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )
    player = await _seed_player(db_session, name="Solo", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=comp,
        player=player,
        year=2024,
        gmsc=12.0,
        minutes=100.0,
        gp=5,
    )

    version_1 = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    v1_rows = await _active_rows(db_session, version_1)
    assert all(r.is_active for r in v1_rows.values())
    v1_slot_row_id = v1_rows["slot:1-4"].id
    v1_mean = v1_rows["slot:1-4"].mean_value

    version_2 = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    assert version_2 != version_1

    v2_rows = await _active_rows(db_session, version_2)
    assert all(r.is_active for r in v2_rows.values())

    # The old version's rows still exist, unmutated in content, but inactive.
    stmt = select(SummerLeagueCohortBaseline).where(
        SummerLeagueCohortBaseline.id == v1_slot_row_id  # type: ignore[arg-type]
    )
    old_row = (await db_session.execute(stmt)).scalar_one()
    assert old_row.is_active is False
    assert old_row.baseline_version == version_1
    assert old_row.mean_value == v1_mean


async def test_build_baselines_game_grain_pools_individual_games(
    db_session: AsyncSession,
) -> None:
    """The `game` grain (#525) pools raw per-game GmSc, gated by the per-game floor.

    Two #1 picks share the ``slot:1-4``/``game:1-4`` cohort. Player A logs
    three qualifying individual games (each its own data point, NOT blended
    into one). Player B logs one game under the per-game minutes floor
    (dropped) and one that clears it. The event grain still builds
    normally alongside the new game grain -- both coexist under the same
    ``baseline_version`` without colliding cohort_keys.
    """
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )
    home = await _seed_team(db_session, comp)
    away = await _seed_team(db_session, comp)

    player_a = await _seed_player(db_session, name="GameA", draft_round=1, draft_pick=1)
    player_b = await _seed_player(db_session, name="GameB", draft_round=1, draft_pick=1)

    # Event grain still needs at least one qualifying blended-season row.
    await _seed_season(
        db_session, competition=comp, player=player_a, year=2024, gmsc=20.0,
        minutes=100.0, gp=5,
    )
    await _seed_season(
        db_session, competition=comp, player=player_b, year=2024, gmsc=8.0,
        minutes=100.0, gp=5,
    )

    source_a = await _seed_source_player(db_session, player=player_a)
    source_b = await _seed_source_player(db_session, player=player_b)

    for pts in (10.0, 20.0, 30.0):
        game = await _seed_game(db_session, comp, home, away)
        await _seed_game_log(
            db_session, competition=comp, game=game, team=home,
            source_player=source_a, player=player_a,
            minutes_seconds=25 * 60, pts=pts,
        )

    thin_game = await _seed_game(db_session, comp, home, away)
    await _seed_game_log(
        db_session, competition=comp, game=thin_game, team=home,
        source_player=source_b, player=player_b,
        minutes_seconds=3 * 60, pts=99.0,  # under the per-game floor -- dropped
    )
    ok_game = await _seed_game(db_session, comp, home, away)
    await _seed_game_log(
        db_session, competition=comp, game=ok_game, team=home,
        source_player=source_b, player=player_b,
        minutes_seconds=15 * 60, pts=8.0,
    )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0, game_min_minutes=10.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    game_row = rows["game:1-4"]
    assert game_row.grain == SummerLeagueDeskGrain.GAME
    assert game_row.cohort_kind == SummerLeagueDeskCohortKind.SLOT_WINDOW
    assert (game_row.slot_low, game_row.slot_high) == (1, 4)
    assert game_row.min_minutes == 10.0
    # Player A's 3 qualifying games + player B's 1 qualifying game == 4;
    # player B's 3-minute game is excluded by the per-game floor.
    assert game_row.n_members == 4
    assert game_row.mean_value == round((10.0 + 20.0 + 30.0 + 8.0) / 4, 2)
    assert game_row.is_active is True

    # The event grain still builds too, unaffected, under the SAME cohort's
    # non-colliding key.
    event_row = rows["slot:1-4"]
    assert event_row.grain == SummerLeagueDeskGrain.EVENT
    assert event_row.n_members == 2
    assert event_row.mean_value == 14.0

"""Integration tests for the Summer League Desk storyline engine (#504).

Seeds a schedule (competition/teams/game/roster), T1 baselines, T2 grades,
and (where relevant) consensus rank / prior-year game logs, then runs
``compute_desk_storylines`` end to end and asserts the persisted T3
(``summer_league_desk_storylines``) + T4 (``summer_league_desk_slate``) rows.
See ``tests/unit/test_sl_desk_storylines.py`` for the pure-detector coverage
(one positive + one near-miss per trigger).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.consensus import BigBoardConsensus, ConsensusSnapshot, ConsensusTrigger
from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrade,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
    SummerLeagueDeskStoryline,
    SummerLeagueDeskTriggerType,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.desk_storylines import compute_desk_storylines

pytestmark = pytest.mark.asyncio

_GAME_DATE = date(2026, 7, 12)
_BASELINE_VERSION = "test-2026.1"
_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


async def _seed_competition(db: AsyncSession, *, year: int = 2026) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas Summer League",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_team(db: AsyncSession, competition: SummerLeagueCompetition) -> SummerLeagueTeamEntry:
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


async def _seed_game(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    *,
    game_date: date = _GAME_DATE,
    status: SummerLeagueGameStatus = SummerLeagueGameStatus.SCHEDULED,
) -> SummerLeagueGame:
    idx = _next_idx()
    assert competition.id is not None
    assert home.id is not None
    assert away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"desk-storyline-game-{idx}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=status,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _seed_player(
    db: AsyncSession, *, name: str, draft_round: int | None, draft_pick: int | None
) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name,
        last_name="Test",
        display_name=f"{name} Test",
        draft_year=2026,
        draft_round=draft_round,
        draft_pick=draft_pick,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _roster_player(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
) -> SummerLeagueSourcePlayer:
    idx = _next_idx()
    assert competition.id is not None
    assert team.id is not None
    assert player.id is not None
    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"src-{idx}",
        raw_player_name=player.display_name or "Test Player",
        normalized_name=(player.display_name or "test player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None

    participation = SummerLeagueParticipation(
        competition_id=competition.id,
        team_entry_id=team.id,
        source_player_id=source_player.id,
        player_id=player.id,
        roster_status=AffiliationStatus.ACTIVE,
    )
    db.add(participation)
    await db.flush()
    return source_player


async def _seed_prior_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    source_player: SummerLeagueSourcePlayer,
    game_date: date,
) -> None:
    """Seed one played prior game (game + box line ~18.4 GmSc) for a streak run."""
    game = await _seed_game(
        db, competition, team, team, game_date=game_date, status=SummerLeagueGameStatus.FINAL
    )
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
        pts=20,
        fgm=8,
        fga=12,
        ftm=4,
        fta=4,
        dreb=5,
        ast=3,
    )
    db.add(log)
    await db.flush()


async def _seed_baseline(
    db: AsyncSession,
    *,
    cohort_key: str,
    cohort_kind: SummerLeagueDeskCohortKind,
    breakpoints: dict[str, float],
    median_value: float,
    mean_value: float,
    grain: SummerLeagueDeskGrain = SummerLeagueDeskGrain.EVENT,
) -> SummerLeagueCohortBaseline:
    baseline = SummerLeagueCohortBaseline(
        baseline_version=_BASELINE_VERSION,
        is_active=True,
        cohort_key=cohort_key,
        cohort_kind=cohort_kind,
        metric="gmsc",
        grain=grain,
        venue_scope="all",
        season_range="2017-2025",
        min_minutes=40.0,
        n_members=20,
        breakpoints=breakpoints,
        mean_value=mean_value,
        median_value=median_value,
    )
    db.add(baseline)
    await db.flush()
    return baseline


async def _seed_grade(
    db: AsyncSession,
    *,
    player: PlayerMaster,
    competition: SummerLeagueCompetition,
    cohort_key: str,
    pctl: float,
    subject_value: float = 15.0,
    gated: bool = False,
) -> SummerLeagueDeskPlayerGrade:
    assert player.id is not None
    assert competition.id is not None
    grade = SummerLeagueDeskPlayerGrade(
        player_id=player.id,
        competition_id=competition.id,
        baseline_version=_BASELINE_VERSION,
        cohort_key=cohort_key,
        subject_value=subject_value,
        pctl=pctl,
        grade=SummerLeagueDeskGrade.HOT if pctl >= 90 else SummerLeagueDeskGrade.MID,
        n_cohort=20,
        gated=gated,
    )
    db.add(grade)
    await db.flush()
    return grade


async def _seed_consensus_rank(db: AsyncSession, *, player: PlayerMaster, rank: int) -> None:
    assert player.id is not None
    snapshot = ConsensusSnapshot(
        draft_year=player.draft_year or 2026,
        computed_at=datetime.utcnow(),
        num_boards=3,
        board_ids=[1, 2, 3],
        trigger=ConsensusTrigger.MANUAL,
    )
    db.add(snapshot)
    await db.flush()
    assert snapshot.id is not None

    consensus_row = BigBoardConsensus(
        snapshot_id=snapshot.id,
        draft_year=player.draft_year or 2026,
        player_id=player.id,
        consensus_rank=rank,
        avg_rank=float(rank),
        median_rank=float(rank),
        high_rank=rank,
        low_rank=rank,
        std_dev=0.0,
        num_sources=3,
    )
    db.add(consensus_row)
    await db.flush()


async def test_compute_desk_storylines_writes_debut_and_status_heat_for_undrafted_player(
    db_session: AsyncSession,
) -> None:
    """An undrafted debutant grading 90th pctl fires both Debut and Status heat."""
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    game = await _seed_game(db_session, competition, home, away)

    player = await _seed_player(db_session, name="Sleeper", draft_round=None, draft_pick=None)
    await _roster_player(db_session, competition, home, player)

    await _seed_baseline(
        db_session,
        cohort_key="status:undrafted",
        cohort_kind=SummerLeagueDeskCohortKind.STATUS,
        breakpoints={"0": 0.0, "50": 10.0, "100": 30.0},
        median_value=10.0,
        mean_value=11.0,
    )
    await _seed_grade(
        db_session,
        player=player,
        competition=competition,
        cohort_key="status:undrafted",
        pctl=90.0,
        subject_value=22.0,
    )

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert len(result.slate) == 1
    assert result.slate[0].game_id == game.id
    assert result.slate[0].is_hero is True
    assert result.slate[0].rank == 1
    assert result.slate[0].total_weight > 0

    trigger_types = {i.trigger_type for i in result.slate[0].instances}
    assert SummerLeagueDeskTriggerType.DEBUT in trigger_types
    assert SummerLeagueDeskTriggerType.STATUS_HEAT in trigger_types

    persisted = (
        (
            await db_session.execute(
                select(SummerLeagueDeskStoryline).where(
                    SummerLeagueDeskStoryline.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    persisted_types = {row.trigger_type for row in persisted}
    assert persisted_types == {
        SummerLeagueDeskTriggerType.DEBUT,
        SummerLeagueDeskTriggerType.STATUS_HEAT,
    }
    for row in persisted:
        assert row.subject_player_id == player.id
        assert row.weight == pytest.approx(row.base_weight * row.magnitude, rel=1e-6)

    slate_row = (
        await db_session.execute(
            select(SummerLeagueDeskSlate).where(
                SummerLeagueDeskSlate.game_id == game.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert slate_row.is_hero is True
    assert slate_row.rank == 1
    assert slate_row.total_weight == pytest.approx(result.slate[0].total_weight)


async def test_compute_desk_storylines_writes_duel_for_two_prominent_prospects(
    db_session: AsyncSession,
) -> None:
    """Two consensus top-14 prospects sharing a game fire the Duel trigger."""
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    game = await _seed_game(db_session, competition, home, away)

    star_a = await _seed_player(db_session, name="StarA", draft_round=1, draft_pick=1)
    star_b = await _seed_player(db_session, name="StarB", draft_round=1, draft_pick=2)
    await _roster_player(db_session, competition, home, star_a)
    await _roster_player(db_session, competition, away, star_b)
    await _seed_consensus_rank(db_session, player=star_a, rank=1)
    await _seed_consensus_rank(db_session, player=star_b, rank=2)

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert len(result.slate) == 1
    duel_instances = [
        i for i in result.slate[0].instances if i.trigger_type == SummerLeagueDeskTriggerType.DUEL
    ]
    assert len(duel_instances) == 1
    duel = duel_instances[0]
    assert {duel.subject_player_id, duel.subject_player_id_2} == {star_a.id, star_b.id}
    assert duel.base_weight == 90.0

    persisted = (
        (
            await db_session.execute(
                select(SummerLeagueDeskStoryline).where(
                    SummerLeagueDeskStoryline.game_id == game.id,  # type: ignore[arg-type]
                    SummerLeagueDeskStoryline.trigger_type
                    == SummerLeagueDeskTriggerType.DUEL,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(persisted) == 1


async def test_compute_desk_storylines_ranks_two_games_and_flags_one_hero(
    db_session: AsyncSession,
) -> None:
    """The higher-weighted game outranks the other; exactly one hero is flagged."""
    competition = await _seed_competition(db_session)

    # Louder game: a #1-overall debut (status heat undrafted 90th pctl too).
    loud_home = await _seed_team(db_session, competition)
    loud_away = await _seed_team(db_session, competition)
    loud_game = await _seed_game(db_session, competition, loud_home, loud_away)
    star = await _seed_player(db_session, name="TopPick", draft_round=1, draft_pick=1)
    await _roster_player(db_session, competition, loud_home, star)
    await _seed_consensus_rank(db_session, player=star, rank=1)

    # Quiet game: nobody rostered, nothing fires.
    quiet_home = await _seed_team(db_session, competition)
    quiet_away = await _seed_team(db_session, competition)
    quiet_game = await _seed_game(db_session, competition, quiet_home, quiet_away)

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert len(result.slate) == 2
    assert loud_game.id is not None
    assert quiet_game.id is not None
    by_game = {row.game_id: row for row in result.slate}
    assert by_game[loud_game.id].is_hero is True
    assert by_game[loud_game.id].rank == 1
    assert by_game[quiet_game.id].is_hero is False
    assert by_game[quiet_game.id].rank == 2
    assert by_game[quiet_game.id].total_weight == 0.0

    slate_rows = (
        (
            await db_session.execute(
                select(SummerLeagueDeskSlate).where(
                    SummerLeagueDeskSlate.competition_id == competition.id,  # type: ignore[arg-type]
                    SummerLeagueDeskSlate.game_date == _GAME_DATE,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(slate_rows) == 2
    hero_rows = [r for r in slate_rows if r.is_hero]
    assert len(hero_rows) == 1
    assert hero_rows[0].game_id == loud_game.id


async def test_compute_desk_storylines_falls_back_to_class_leader_when_no_games_today(
    db_session: AsyncSession,
) -> None:
    """No games today -> empty slate + a quiet-slate class-leader hero (spec §4)."""
    competition = await _seed_competition(db_session)

    leader = await _seed_player(db_session, name="ClassLeader", draft_round=1, draft_pick=1)
    also_ran = await _seed_player(db_session, name="AlsoRan", draft_round=2, draft_pick=10)
    await _seed_grade(
        db_session,
        player=leader,
        competition=competition,
        cohort_key="slot:1-4",
        pctl=97.0,
        subject_value=25.0,
    )
    await _seed_grade(
        db_session,
        player=also_ran,
        competition=competition,
        cohort_key="round:2",
        pctl=55.0,
        subject_value=12.0,
    )

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert result.slate == []
    assert result.quiet_slate_hero is not None
    assert result.quiet_slate_hero.kind == "class_leader"
    assert result.quiet_slate_hero.player_id == leader.id


async def test_compute_desk_storylines_second_look_for_returning_player_and_no_debut(
    db_session: AsyncSession,
) -> None:
    """A returner with a notable swing fires 2nd look, never Debut."""
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(db_session, competition, home, away)

    player = await _seed_player(db_session, name="Sophomore", draft_round=1, draft_pick=10)
    await _roster_player(db_session, competition, home, player)

    # Prior-year SL season (makes this a returner, not a debutant).
    assert player.id is not None
    prior_competition = await _seed_competition(db_session, year=2025)
    prior_season = SummerLeaguePlayerSeason(
        competition_id=prior_competition.id,
        player_id=player.id,
        year=2025,
        venue_slug="las_vegas",
        gp=5,
        minutes=100.0,
        gmsc=8.0,
    )
    db_session.add(prior_season)
    await db_session.flush()

    baseline = await _seed_baseline(
        db_session,
        cohort_key="slot:8-11",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        breakpoints={"0": 0.0, "25": 6.0, "50": 10.0, "75": 16.0, "100": 24.0},
        median_value=10.0,
        mean_value=10.5,
    )
    await _seed_grade(
        db_session,
        player=player,
        competition=competition,
        cohort_key=baseline.cohort_key,
        pctl=70.0,
        subject_value=18.0,
    )

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert len(result.slate) == 1
    trigger_types = {i.trigger_type for i in result.slate[0].instances}
    assert SummerLeagueDeskTriggerType.SECOND_LOOK in trigger_types
    assert SummerLeagueDeskTriggerType.DEBUT not in trigger_types


async def test_compute_desk_storylines_rerun_replaces_rather_than_duplicates_t3(
    db_session: AsyncSession,
) -> None:
    """Re-running the tick for the same day fully replaces T3, never accumulates."""
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    game = await _seed_game(db_session, competition, home, away)

    player = await _seed_player(db_session, name="Repeat", draft_round=None, draft_pick=None)
    await _roster_player(db_session, competition, home, player)
    await _seed_baseline(
        db_session,
        cohort_key="status:undrafted",
        cohort_kind=SummerLeagueDeskCohortKind.STATUS,
        breakpoints={"0": 0.0, "50": 10.0, "100": 30.0},
        median_value=10.0,
        mean_value=11.0,
    )
    await _seed_grade(
        db_session,
        player=player,
        competition=competition,
        cohort_key="status:undrafted",
        pctl=92.0,
        subject_value=20.0,
    )

    await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )
    await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    persisted = (
        (
            await db_session.execute(
                select(SummerLeagueDeskStoryline).where(
                    SummerLeagueDeskStoryline.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    # DEBUT + STATUS_HEAT, not double-counted across the two ticks.
    assert len(persisted) == 2

    slate_rows = (
        (
            await db_session.execute(
                select(SummerLeagueDeskSlate).where(
                    SummerLeagueDeskSlate.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(slate_rows) == 1


async def test_compute_desk_storylines_writes_streak_from_prior_game_logs(
    db_session: AsyncSession,
) -> None:
    """A run of 3 prior games clearing the game-grain cohort median fires Streak.

    #525: each prior game's GmSc is ranked against the player's cohort's
    *game-grain* T1 breakpoints (``percentile_of_value``) with the bar =
    that row's ``median_value`` -- the correct single-game distribution,
    not the event-aggregate one.
    """
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    today_game = await _seed_game(db_session, competition, home, away)

    player = await _seed_player(db_session, name="Streaker", draft_round=1, draft_pick=1)
    source_player = await _roster_player(db_session, competition, home, player)

    # Breakpoints chosen so the seeded ~18.4-GmSc games rank >= 65th pctl and
    # clear the 10.0 cohort median -> 3 straight qualifying games -> a streak.
    # Game grain (`game:1-4`), not event grain -- the streak trigger ranks
    # each individual game against the game-grain T1 row (#525).
    await _seed_baseline(
        db_session,
        cohort_key="game:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        breakpoints={"0": 0.0, "50": 10.0, "65": 15.0, "100": 30.0},
        median_value=10.0,
        mean_value=12.0,
        grain=SummerLeagueDeskGrain.GAME,
    )
    await _seed_grade(
        db_session,
        player=player,
        competition=competition,
        cohort_key="slot:1-4",
        pctl=73.0,
        subject_value=18.4,
    )

    for day in (8, 9, 10):
        await _seed_prior_game_log(
            db_session,
            competition=competition,
            team=home,
            player=player,
            source_player=source_player,
            game_date=date(2026, 7, day),
        )

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert len(result.slate) == 1
    trigger_types = {i.trigger_type for i in result.slate[0].instances}
    assert SummerLeagueDeskTriggerType.STREAK in trigger_types

    persisted = (
        (
            await db_session.execute(
                select(SummerLeagueDeskStoryline).where(
                    SummerLeagueDeskStoryline.game_id == today_game.id,  # type: ignore[arg-type]
                    SummerLeagueDeskStoryline.trigger_type
                    == SummerLeagueDeskTriggerType.STREAK,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(persisted) == 1
    assert persisted[0].subject_player_id == player.id
    assert persisted[0].base_weight == 65.0


async def test_compute_desk_storylines_streak_reads_game_grain_not_event_grain(
    db_session: AsyncSession,
) -> None:
    """A hostile event-grain row (that would fail the streak) proves the game grain wins.

    Seeds BOTH an event-grain baseline whose median (25.0) the seeded
    ~18.4-GmSc games would NOT clear, and a game-grain baseline whose median
    (10.0) they DO clear. If the streak trigger were still (mis)reading the
    event-grain row, no run would qualify and Streak would never fire.
    """
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    await _seed_game(db_session, competition, home, away)

    player = await _seed_player(
        db_session, name="GrainCheck", draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)

    # Event grain: median 25.0 -- the ~18.4 GmSc games fall BELOW this, which
    # would break any run if this were the row consulted.
    await _seed_baseline(
        db_session,
        cohort_key="slot:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        breakpoints={"0": 10.0, "50": 25.0, "65": 28.0, "100": 40.0},
        median_value=25.0,
        mean_value=25.0,
        grain=SummerLeagueDeskGrain.EVENT,
    )
    # Game grain: median 10.0 -- the games clear it and rank >= 65th pctl.
    await _seed_baseline(
        db_session,
        cohort_key="game:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        breakpoints={"0": 0.0, "50": 10.0, "65": 15.0, "100": 30.0},
        median_value=10.0,
        mean_value=12.0,
        grain=SummerLeagueDeskGrain.GAME,
    )
    await _seed_grade(
        db_session,
        player=player,
        competition=competition,
        cohort_key="slot:1-4",
        pctl=73.0,
        subject_value=18.4,
    )

    for day in (8, 9, 10):
        await _seed_prior_game_log(
            db_session,
            competition=competition,
            team=home,
            player=player,
            source_player=source_player,
            game_date=date(2026, 7, day),
        )

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )

    assert len(result.slate) == 1
    trigger_types = {i.trigger_type for i in result.slate[0].instances}
    assert SummerLeagueDeskTriggerType.STREAK in trigger_types

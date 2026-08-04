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
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
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
from app.schemas.summer_league_metrics import SummerLeagueDerivedAgg
from app.services.summer_league.desk_storylines import compute_desk_storylines

pytestmark = pytest.mark.asyncio

_GAME_DATE = date(2026, 7, 12)
_BASELINE_VERSION = "test-2026.1"
_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


async def _seed_competition(
    db: AsyncSession, *, year: int = 2026
) -> SummerLeagueEdition:
    comp = SummerLeagueEdition(
        year=year,
        league_id="15",
        venue_slug="las_vegas",
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


async def _seed_game(
    db: AsyncSession,
    competition: SummerLeagueEdition,
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
    competition: SummerLeagueEdition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
) -> SummerLeagueSourceRecord:
    idx = _next_idx()
    assert competition.id is not None
    assert team.id is not None
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


async def _seed_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    source_player: SummerLeagueSourceRecord,
    minutes_seconds: int = 25 * 60,
    pts: float = 15.0,
) -> SummerLeaguePlayerGameLog:
    """One qualifying box line (GmSc == ``pts``) attached to an EXISTING game.

    Unlike ``_seed_prior_game_log`` (which creates its own game), this
    attaches to a game the caller already has -- e.g. tonight's game, so a
    player's debut trigger (#539: fires only when the current game is their
    canonical first-qualifying game) can actually clear the per-game minutes
    floor on the same game being evaluated.
    """
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


async def _seed_prior_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    source_player: SummerLeagueSourceRecord,
    game_date: date,
) -> None:
    """Seed one played prior game (game + box line ~18.4 GmSc) for a streak run."""
    game = await _seed_game(
        db,
        competition,
        team,
        team,
        game_date=game_date,
        status=SummerLeagueGameStatus.FINAL,
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
    competition: SummerLeagueEdition,
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


async def _seed_consensus_rank(
    db: AsyncSession, *, player: PlayerMaster, rank: int
) -> None:
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

    player = await _seed_player(
        db_session, name="Sleeper", draft_round=None, draft_pick=None
    )
    source_player = await _roster_player(db_session, competition, home, player)
    # Debut (#539) fires only when tonight's game is the subject's canonical
    # first-qualifying game -- seed one qualifying box line on `game` itself.
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        player=player,
        source_player=source_player,
    )

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
        i
        for i in result.slate[0].instances
        if i.trigger_type == SummerLeagueDeskTriggerType.DUEL
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

    leader = await _seed_player(
        db_session, name="ClassLeader", draft_round=1, draft_pick=1
    )
    also_ran = await _seed_player(
        db_session, name="AlsoRan", draft_round=2, draft_pick=10
    )
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

    player = await _seed_player(
        db_session, name="Sophomore", draft_round=1, draft_pick=10
    )
    await _roster_player(db_session, competition, home, player)

    # Prior-year SL season (makes this a returner, not a debutant).
    assert player.id is not None
    prior_competition = await _seed_competition(db_session, year=2025)
    prior_season = SummerLeagueDerivedAgg(
        competition_id=prior_competition.id,
        player_id=player.id,
        year=2025,
        venue_slug="las_vegas",
        is_current=True,
        gp=5,
        minutes=100.0,
        gmsc=8.0,
    )
    db_session.add(prior_season)
    await db_session.flush()

    # #539: Debut now reads a real qualifying GAME log, not just the season
    # aggregate -- back the prior-year season with one actual qualifying
    # game so the shared first-qualifying-game lookup sees this player as
    # already debuted (a season aggregate with no underlying game log would
    # never happen in real, materialized-from-logs data).
    prior_home = await _seed_team(db_session, prior_competition)
    prior_away = await _seed_team(db_session, prior_competition)
    prior_source_player = await _roster_player(
        db_session, prior_competition, prior_home, player
    )
    prior_game = await _seed_game(
        db_session,
        prior_competition,
        prior_home,
        prior_away,
        game_date=date(2025, 7, 10),
    )
    await _seed_game_log(
        db_session,
        competition=prior_competition,
        game=prior_game,
        team=prior_home,
        player=player,
        source_player=prior_source_player,
    )

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


async def test_compute_desk_storylines_debut_fires_once_not_on_every_game_of_the_season(
    db_session: AsyncSession,
) -> None:
    """#539 DoD: the first qualifying game fires Debut; the second one cannot.

    Seeds a debutant with TWO qualifying games this event -- an earlier one
    (day 1) and a later one (day 2, evaluated as its own separate
    ``compute_desk_storylines`` tick, mirroring two different hourly ticks on
    two different nights). Only day 1's tick sees the Debut trigger; day 2's
    tick -- for the SAME player, same cohort -- must not re-fire it, proving
    the shared ``first_qualifying_games`` lookup pins the debut to exactly
    one game rather than the player's whole debut season.
    """
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)

    day1 = date(2026, 7, 10)
    day2 = date(2026, 7, 12)  # == _GAME_DATE
    game1 = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=day1,
        status=SummerLeagueGameStatus.FINAL,
    )
    game2 = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=day2,
        status=SummerLeagueGameStatus.FINAL,
    )

    player = await _seed_player(
        db_session, name="TwoGameDebut", draft_round=1, draft_pick=1
    )
    source_player = await _roster_player(db_session, competition, home, player)

    # Both games clear the per-game qualifying floor; game1 is chronologically
    # first, so it -- not game2 -- is the canonical debut game.
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game1,
        team=home,
        player=player,
        source_player=source_player,
        minutes_seconds=20 * 60,
        pts=14.0,
    )
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game2,
        team=home,
        player=player,
        source_player=source_player,
        minutes_seconds=22 * 60,
        pts=18.0,
    )

    day1_result = await compute_desk_storylines(
        db_session,
        game_date=day1,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )
    day1_trigger_types = {i.trigger_type for i in day1_result.slate[0].instances}
    assert SummerLeagueDeskTriggerType.DEBUT in day1_trigger_types

    day2_result = await compute_desk_storylines(
        db_session,
        game_date=day2,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="morning",
    )
    day2_trigger_types = {i.trigger_type for i in day2_result.slate[0].instances}
    assert SummerLeagueDeskTriggerType.DEBUT not in day2_trigger_types


async def test_compute_desk_storylines_rerun_replaces_rather_than_duplicates_t3(
    db_session: AsyncSession,
) -> None:
    """Re-running the tick for the same day fully replaces T3, never accumulates."""
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    game = await _seed_game(db_session, competition, home, away)

    player = await _seed_player(
        db_session, name="Repeat", draft_round=None, draft_pick=None
    )
    source_player = await _roster_player(db_session, competition, home, player)
    # Debut (#539) fires only when tonight's game is the subject's canonical
    # first-qualifying game -- seed one qualifying box line on `game` itself.
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        player=player,
        source_player=source_player,
    )
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

    player = await _seed_player(
        db_session, name="Streaker", draft_round=1, draft_pick=1
    )
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


# --------------------------------------------------------------------------- #
# #541 -- Live ordering by tonight's realized game-grain deviation
# --------------------------------------------------------------------------- #
async def test_compute_desk_storylines_live_orders_by_realized_tonight_line_not_entering_weight(
    db_session: AsyncSession,
) -> None:
    """Live mode ranks games by tonight's REAL box line, not the Morning entering weight.

    Both players are #1-overall debutants (identical Morning/entering weight:
    Debut fires on both at the same magnitude), so an entering-weight-only
    ranker would tie them. Their tonight's canonical GmSc lines rank very
    differently against the active game-grain cohort baseline -- Live mode
    must use THAT (`GameSlateInput.live_deviation`, #541) to pick a winner.
    """
    competition = await _seed_competition(db_session)

    weak_home = await _seed_team(db_session, competition)
    weak_away = await _seed_team(db_session, competition)
    weak_game = await _seed_game(
        db_session,
        competition,
        weak_home,
        weak_away,
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    strong_home = await _seed_team(db_session, competition)
    strong_away = await _seed_team(db_session, competition)
    strong_game = await _seed_game(
        db_session,
        competition,
        strong_home,
        strong_away,
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )

    weak_player = await _seed_player(
        db_session, name="WeakLine", draft_round=1, draft_pick=1
    )
    weak_source = await _roster_player(db_session, competition, weak_home, weak_player)
    strong_player = await _seed_player(
        db_session, name="StrongLine", draft_round=1, draft_pick=1
    )
    strong_source = await _roster_player(
        db_session, competition, strong_home, strong_player
    )

    # Game-grain baseline both players' cohort ("game:1-4") ranks against.
    await _seed_baseline(
        db_session,
        cohort_key="game:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        breakpoints={"0": 0.0, "50": 10.0, "100": 30.0},
        median_value=10.0,
        mean_value=12.0,
        grain=SummerLeagueDeskGrain.GAME,
    )

    # Tonight's REAL canonical lines (GmSc == pts on this fixture -- other box
    # components are 0): weak_player sits at the cohort's 50th pctl (0
    # deviation); strong_player sits near the top (large deviation).
    await _seed_game_log(
        db_session,
        competition=competition,
        game=weak_game,
        team=weak_home,
        player=weak_player,
        source_player=weak_source,
        pts=10.0,
    )
    await _seed_game_log(
        db_session,
        competition=competition,
        game=strong_game,
        team=strong_home,
        player=strong_player,
        source_player=strong_source,
        pts=25.0,
    )

    result = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="live",
    )

    assert len(result.slate) == 2
    assert strong_game.id is not None
    assert weak_game.id is not None
    by_game = {row.game_id: row for row in result.slate}
    assert by_game[strong_game.id].is_hero is True
    assert by_game[strong_game.id].rank == 1
    assert by_game[weak_game.id].is_hero is False
    # 25 GmSc interpolates to pctl 87.5 -> deviation 37.5; 10 GmSc is exactly
    # the cohort median (pctl 50) -> deviation 0.
    assert by_game[strong_game.id].total_weight == pytest.approx(37.5)
    assert by_game[weak_game.id].total_weight == pytest.approx(0.0)

    persisted_slate = {
        row.game_id: row
        for row in (
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
    }
    assert persisted_slate[strong_game.id].is_hero is True
    assert persisted_slate[weak_game.id].is_hero is False


async def test_compute_desk_storylines_live_reorders_on_next_tick_when_a_line_changes(
    db_session: AsyncSession,
) -> None:
    """DoD: changing one player's line changes the NEXT tick's order.

    Runs the exact same `compute_desk_storylines(mode="live")` call twice
    over the SAME two games -- mirroring two consecutive hourly ticks -- with
    only one game's player's canonical box line mutated in between
    (simulating a fresh live-refresh landing new box-score data, #530).
    """
    competition = await _seed_competition(db_session)

    home_a = await _seed_team(db_session, competition)
    away_a = await _seed_team(db_session, competition)
    game_a = await _seed_game(
        db_session,
        competition,
        home_a,
        away_a,
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )
    home_b = await _seed_team(db_session, competition)
    away_b = await _seed_team(db_session, competition)
    game_b = await _seed_game(
        db_session,
        competition,
        home_b,
        away_b,
        status=SummerLeagueGameStatus.IN_PROGRESS,
    )

    player_a = await _seed_player(
        db_session, name="PlayerA", draft_round=1, draft_pick=1
    )
    source_a = await _roster_player(db_session, competition, home_a, player_a)
    player_b = await _seed_player(
        db_session, name="PlayerB", draft_round=1, draft_pick=1
    )
    source_b = await _roster_player(db_session, competition, home_b, player_b)

    await _seed_baseline(
        db_session,
        cohort_key="game:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        breakpoints={"0": 0.0, "50": 10.0, "100": 30.0},
        median_value=10.0,
        mean_value=12.0,
        grain=SummerLeagueDeskGrain.GAME,
    )

    log_a = await _seed_game_log(
        db_session,
        competition=competition,
        game=game_a,
        team=home_a,
        player=player_a,
        source_player=source_a,
        pts=12.0,  # pctl ~55 -> small deviation
    )
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game_b,
        team=home_b,
        player=player_b,
        source_player=source_b,
        pts=18.0,  # pctl 70 -> deviation 20, outranks game_a on tick 1
    )

    assert game_a.id is not None
    assert game_b.id is not None

    tick1 = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="live",
    )
    by_game_tick1 = {row.game_id: row for row in tick1.slate}
    assert by_game_tick1[game_b.id].is_hero is True
    assert by_game_tick1[game_a.id].is_hero is False

    # Next tick: player_a's canonical line refreshes to a much hotter line --
    # nothing about game_b changes.
    log_a.pts = 29  # pctl 97.5 -> deviation 47.5, now the biggest in the slate
    db_session.add(log_a)
    await db_session.flush()

    tick2 = await compute_desk_storylines(
        db_session,
        game_date=_GAME_DATE,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version=_BASELINE_VERSION,
        mode="live",
    )
    by_game_tick2 = {row.game_id: row for row in tick2.slate}
    assert by_game_tick2[game_a.id].is_hero is True
    assert by_game_tick2[game_b.id].is_hero is False


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


async def test_compute_desk_storylines_live_excludes_dnp_roster_shell(
    db_session: AsyncSession,
) -> None:
    """Live mode drops a rostered player who didn't actually appear (DNP shell).

    Reproduces the Cam Reddish Live-hero hijack: a rostered veteran who dressed
    but sat has a NULL-minutes box shell tonight, yet -- graded and debut-
    eligible -- would otherwise fire a storyline and could outweigh everyone who
    actually played. Both players here carry an identical grade + cohort
    baseline, so absent the participation gate BOTH would be eligible subjects.
    Live mode must generate storylines only for players who took the floor: the
    teammate who played gets one, the DNP shell gets none.
    """
    competition = await _seed_competition(db_session)
    home = await _seed_team(db_session, competition)
    away = await _seed_team(db_session, competition)
    game = await _seed_game(
        db_session, competition, home, away, status=SummerLeagueGameStatus.IN_PROGRESS
    )

    await _seed_baseline(
        db_session,
        cohort_key="status:undrafted",
        cohort_kind=SummerLeagueDeskCohortKind.STATUS,
        breakpoints={"0": 0.0, "50": 10.0, "100": 30.0},
        median_value=10.0,
        mean_value=11.0,
    )

    played = await _seed_player(
        db_session, name="Played", draft_round=None, draft_pick=None
    )
    played_src = await _roster_player(db_session, competition, home, played)
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        player=played,
        source_player=played_src,
    )
    await _seed_grade(
        db_session,
        player=played,
        competition=competition,
        cohort_key="status:undrafted",
        pctl=90.0,
        subject_value=22.0,
    )

    dnp = await _seed_player(
        db_session, name="Satout", draft_round=None, draft_pick=None
    )
    dnp_src = await _roster_player(db_session, competition, home, dnp)
    # DNP roster shell on tonight's game: NULL minutes and box stats, exactly as
    # the NBA feed records a player who dressed but never checked in.
    assert competition.id is not None and game.id is not None
    assert home.id is not None and dnp_src.id is not None and dnp.id is not None
    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,
            game_id=game.id,
            team_entry_id=home.id,
            source_player_id=dnp_src.id,
            player_id=dnp.id,
            nba_stats_person_id="srcpid-dnp",
            raw_player_name=dnp.display_name or "DNP",
            minutes_seconds=None,
        )
    )
    await db_session.flush()
    await _seed_grade(
        db_session,
        player=dnp,
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
        mode="live",
    )
    assert result.slate  # the played teammate keeps the game on the slate

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
    subjects = {row.subject_player_id for row in persisted}
    assert played.id in subjects, "the player who took the floor should still fire"
    assert dnp.id not in subjects, "the DNP roster shell must not be a subject"

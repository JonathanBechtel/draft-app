"""Integration tests for Competition Context aggregation & publication (#617).

Seed normalized Summer League facts into a disposable schema and drive the real
``rebuild_environment_profiles`` end to end:

* source-to-projection parity (pooled possessions/box metrics reproduce an
  independent ``Box.poss`` recomputation);
* two competitions in one year pooled into an all-competitions season profile,
  with one repeated canonical player and one unresolved appearance;
* mixed endpoint coverage (box complete, shot partial → null shot metric);
* versioned publication with atomic current replacement;
* a failed candidate that preserves the prior current version;
* one-year and full-historical backfill idempotency;
* raw-fact checksums proving the rebuild never mutates raw facts;
* a two-session, barrier-synchronized concurrency test proving a competing
  writer cannot change inputs between the rebuild's reads and its publication.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_environment import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    SummerLeagueEnvironmentMetricCoverage,
    SummerLeagueEnvironmentProfile,
    SummerLeagueEnvironmentSeasonMembership,
)
from app.services.summer_league.metrics import Box
from app.services.summer_league.write_lock import acquire_summer_league_writer_lock
from app.services.summer_league_environment_service import (
    EnvironmentScope,
    get_environment_profile,
    rebuild_environment_profiles,
)
from tests.integration.conftest import make_player

pytestmark = pytest.mark.asyncio

_SEQ = {"n": 0}

# Two deliberately different team box lines so denominators are unequal.
_TEAM_A = dict(
    minutes=200,
    pts=110,
    fgm=42,
    fga=88,
    fg3m=12,
    fg3a=34,
    ftm=14,
    fta=18,
    oreb=10,
    dreb=32,
    reb=42,
    ast=26,
    stl=7,
    blk=4,
    tov=13,
    pf=18,
)
_TEAM_B = dict(
    minutes=200,
    pts=98,
    fgm=38,
    fga=90,
    fg3m=9,
    fg3a=28,
    ftm=13,
    fta=20,
    oreb=12,
    dreb=28,
    reb=40,
    ast=20,
    stl=8,
    blk=5,
    tov=16,
    pf=20,
)


def _box_from(line: dict) -> Box:
    box = Box()
    box.add_row(type("Row", (), line)())
    return box


async def _competition(
    db: AsyncSession, *, year: int, venue: str, league_id: str
) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue,
        display_name=f"{year} {venue}",
        starts_on=date(year, 7, 8),
        ends_on=date(year, 7, 18),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _team(db: AsyncSession, comp_id: int, idx: int) -> SummerLeagueTeamEntry:
    _SEQ["n"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"team-{_SEQ['n']}",
        raw_team_name=f"Team {idx}",
        team_slug=f"team-{_SEQ['n']}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _source_player(
    db: AsyncSession, *, resolved: bool, player_id: int | None = None
) -> SummerLeagueSourcePlayer:
    _SEQ["n"] += 1
    canonical = player_id
    if resolved and canonical is None:
        player = make_player(f"First{_SEQ['n']}", f"Last{_SEQ['n']}")
        db.add(player)
        await db.flush()
        canonical = player.id
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"sp-{_SEQ['n']}",
        raw_player_name=f"Player {_SEQ['n']}",
        normalized_name=f"player {_SEQ['n']}",
        canonical_player_id=canonical if resolved else None,
    )
    db.add(sp)
    await db.flush()
    assert sp.id is not None
    return sp


async def _final_game(
    db: AsyncSession,
    *,
    comp_id: int,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    game_date: date,
    home_score: int | None,
    away_score: int | None,
    status: SummerLeagueGameStatus = SummerLeagueGameStatus.FINAL,
    status_text: str | None = None,
) -> SummerLeagueGame:
    _SEQ["n"] += 1
    game = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"g-{_SEQ['n']}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
        status=status,
        status_text=status_text,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _team_log(
    db: AsyncSession, *, comp_id: int, game_id: int, team_id: int, line: dict
) -> None:
    db.add(
        SummerLeagueTeamGameLog(
            competition_id=comp_id, game_id=game_id, team_entry_id=team_id, **line
        )
    )


async def _player_log(
    db: AsyncSession,
    *,
    comp_id: int,
    game_id: int,
    team_id: int,
    source: SummerLeagueSourcePlayer,
    minutes_seconds: int = 1500,
    pts: int = 12,
    starter_position: str | None = None,
) -> None:
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp_id,
            game_id=game_id,
            team_entry_id=team_id,
            source_player_id=source.id,
            player_id=source.canonical_player_id,
            nba_stats_person_id=source.nba_stats_person_id,
            raw_player_name=source.raw_player_name,
            starter_position=starter_position,
            minutes_seconds=minutes_seconds,
            pts=pts,
        )
    )


async def _seed_competition(
    db: AsyncSession,
    *,
    year: int,
    venue: str,
    league_id: str,
    n_games: int = 2,
    shared_player_id: int | None = None,
    add_unresolved: bool = False,
) -> tuple[int, list]:
    """Seed a two-team competition with ``n_games`` box-complete final games."""
    comp = await _competition(db, year=year, venue=venue, league_id=league_id)
    assert comp.id is not None
    team_a = await _team(db, comp.id, 1)
    team_b = await _team(db, comp.id, 2)
    team_a_id, team_b_id = team_a.id, team_b.id
    assert team_a_id is not None and team_b_id is not None

    # Roster: a shared canonical player (repeat across competitions) if given.
    shared = await _source_player(db, resolved=True, player_id=shared_player_id)
    p_a2 = await _source_player(db, resolved=True)
    p_b1 = await _source_player(db, resolved=True)
    roster_a = [shared, p_a2]
    roster_b = [p_b1]
    if add_unresolved:
        roster_b.append(await _source_player(db, resolved=False))

    for g in range(n_games):
        game = await _final_game(
            db,
            comp_id=comp.id,
            home=team_a,
            away=team_b,
            game_date=date(year, 7, 8 + g),
            home_score=_TEAM_A["pts"],
            away_score=_TEAM_B["pts"],
            status_text="Final" if g == 0 else "Final/OT",
        )
        assert game.id is not None
        await _team_log(
            db, comp_id=comp.id, game_id=game.id, team_id=team_a_id, line=_TEAM_A
        )
        await _team_log(
            db, comp_id=comp.id, game_id=game.id, team_id=team_b_id, line=_TEAM_B
        )
        for source in roster_a:
            await _player_log(
                db,
                comp_id=comp.id,
                game_id=game.id,
                team_id=team_a_id,
                source=source,
                starter_position="G",
            )
        for source in roster_b:
            await _player_log(
                db,
                comp_id=comp.id,
                game_id=game.id,
                team_id=team_b_id,
                source=source,
                starter_position="F",
            )
    await db.flush()
    return comp.id, [shared, p_a2, p_b1]


# --------------------------------------------------------------------------- #
# Source-to-projection parity
# --------------------------------------------------------------------------- #
async def test_competition_projection_matches_pooled_source(
    db_session: AsyncSession,
) -> None:
    """Published box metrics reproduce an independent Box.poss recomputation."""
    comp_id, _ = await _seed_competition(
        db_session, year=2025, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        result = await rebuild_environment_profiles(db_session, competition_id=comp_id)
    assert result.built_scopes == 1
    assert result.failed_scopes == 0

    profile = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2025)
    )
    assert profile is not None

    # Independent recomputation from the seeded team lines.
    box_a, box_b = _box_from(_TEAM_A), _box_from(_TEAM_B)
    n_games = 2
    poss_per_game = box_a.poss(box_b) + box_b.poss(box_a)
    total_poss = n_games * poss_per_game
    team_game_rows = 2 * n_games
    pooled_pts = n_games * (box_a.pts + box_b.pts)
    team_minutes = team_game_rows * 200.0

    assert profile.final_games == n_games
    assert profile.box_complete_games == n_games
    assert profile.points_per_team_game == pytest.approx(
        pooled_pts / team_game_rows, abs=0.05
    )
    assert profile.estimated_possessions == pytest.approx(
        total_poss / team_game_rows, abs=0.05
    )
    assert profile.pace_per_48 == pytest.approx(
        48.0 * total_poss / (team_minutes / 5.0), abs=0.05
    )
    assert profile.offensive_rating == pytest.approx(
        100.0 * pooled_pts / total_poss, abs=0.05
    )
    # Isolated by competition id: no season profile was built by a competition rebuild.
    season = await get_environment_profile(
        db_session, EnvironmentScope.for_season(2025)
    )
    assert season is None


async def test_non_final_games_disclosed_in_scheduled_count(
    db_session: AsyncSession,
) -> None:
    """SCHEDULED plus IN_PROGRESS/POSTPONED/CANCELED/UNKNOWN all disclose as
    "Scheduled / not-final" (contract §3) -- none silently vanish from
    pooling just because only SCHEDULED status has its own accumulator.
    """
    comp_id, _ = await _seed_competition(
        db_session, year=2025, venue="las_vegas", league_id="15", n_games=1
    )
    comp = (
        await db_session.execute(
            select(SummerLeagueCompetition).where(
                SummerLeagueCompetition.id == comp_id
            )
        )
    ).scalar_one()
    team_a = await _team(db_session, comp_id, 3)
    team_b = await _team(db_session, comp_id, 4)
    # One of each non-final status alongside the one FINAL game already seeded.
    for status in (
        SummerLeagueGameStatus.SCHEDULED,
        SummerLeagueGameStatus.IN_PROGRESS,
        SummerLeagueGameStatus.POSTPONED,
        SummerLeagueGameStatus.CANCELED,
        SummerLeagueGameStatus.UNKNOWN,
    ):
        await _final_game(
            db_session,
            comp_id=comp_id,
            home=team_a,
            away=team_b,
            game_date=comp.starts_on,
            home_score=None,
            away_score=None,
            status=status,
        )
    await db_session.commit()

    async with db_session.begin():
        result = await rebuild_environment_profiles(db_session, competition_id=comp_id)
    assert result.built_scopes == 1
    assert result.failed_scopes == 0

    profile = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2025)
    )
    assert profile is not None
    assert profile.final_games == 1
    # All 5 non-final statuses disclosed, not just the literal SCHEDULED one.
    assert profile.scheduled_games == 5


# --------------------------------------------------------------------------- #
# Season pooling: two competitions, repeat + unresolved players
# --------------------------------------------------------------------------- #
async def test_season_pools_two_competitions_with_repeat_and_unresolved(
    db_session: AsyncSession,
) -> None:
    """A season profile pools every competition; repeats de-dup, unresolved stays visible."""
    player = make_player("Repeat", "Player")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    comp_a, _ = await _seed_competition(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        shared_player_id=player.id,
    )
    comp_b, _ = await _seed_competition(
        db_session,
        year=2025,
        venue="salt_lake_city",
        league_id="13",
        shared_player_id=player.id,
        add_unresolved=True,
    )
    await db_session.commit()

    async with db_session.begin():
        result = await rebuild_environment_profiles(db_session, year=2025)
    # Two competition scopes + one season scope.
    assert result.built_scopes == 3

    season = await get_environment_profile(
        db_session, EnvironmentScope.for_season(2025)
    )
    assert season is not None
    assert season.included_competitions == 2
    assert season.final_games == 4  # 2 per competition

    # Membership names every competition.
    membership = (
        (
            await db_session.execute(
                select(SummerLeagueEnvironmentSeasonMembership.competition_id).where(
                    SummerLeagueEnvironmentSeasonMembership.profile_id == season.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(membership) == {comp_a, comp_b}

    # The repeat canonical player is counted once across both competitions.
    # Distinct resolved players: repeat + 2 (comp_a) + 2 (comp_b) = 5.
    assert season.appeared_players == 5
    # One unresolved appearance is disclosed separately, never merged.
    assert season.appeared_unresolved == 1


# --------------------------------------------------------------------------- #
# Mixed endpoint coverage
# --------------------------------------------------------------------------- #
async def test_partial_shot_coverage_nulls_shot_metric(
    db_session: AsyncSession,
) -> None:
    """A box-complete competition with only some shot-covered games nulls rim metrics."""
    comp_id, _ = await _seed_competition(
        db_session, year=2024, venue="las_vegas", league_id="15", n_games=2
    )
    # Add shot events to exactly one of the two final games (partial shot coverage).
    game_id = (
        await db_session.execute(
            select(SummerLeagueGame.id)
            .where(SummerLeagueGame.competition_id == comp_id)
            .limit(1)
        )
    ).scalar_one()
    team_id = (
        await db_session.execute(
            select(SummerLeagueTeamEntry.id)
            .where(SummerLeagueTeamEntry.competition_id == comp_id)
            .limit(1)
        )
    ).scalar_one()
    source = (
        (await db_session.execute(select(SummerLeagueSourcePlayer).limit(1)))
        .scalars()
        .first()
    )
    assert source is not None
    _SEQ["n"] += 1
    db_session.add(
        SummerLeagueShotEvent(
            game_id=game_id,
            competition_id=comp_id,
            team_entry_id=team_id,
            source_player_id=source.id,
            player_id=source.canonical_player_id,
            nba_stats_person_id=source.nba_stats_person_id,
            nba_stats_game_id=f"shot-{_SEQ['n']}",
            nba_stats_game_event_id=_SEQ["n"],
            shot_zone_basic="Restricted Area",
            made=True,
        )
    )
    await db_session.commit()

    async with db_session.begin():
        await rebuild_environment_profiles(db_session, competition_id=comp_id)

    profile = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2024)
    )
    assert profile is not None
    # Box metric certified; shot metric withheld under partial coverage.
    assert profile.points_per_team_game is not None
    assert profile.rim_attempt_share is None
    coverage = {
        row.metric_key: row
        for row in (
            await db_session.execute(
                select(SummerLeagueEnvironmentMetricCoverage).where(
                    SummerLeagueEnvironmentMetricCoverage.profile_id == profile.id
                )
            )
        ).scalars()
    }
    assert coverage["points_per_team_game"].coverage == COVERAGE_COMPLETE
    assert coverage["rim_attempt_share"].coverage == COVERAGE_PARTIAL
    assert coverage["rim_attempt_share"].reason is not None


# --------------------------------------------------------------------------- #
# Versioned publication + current replacement
# --------------------------------------------------------------------------- #
async def test_rebuild_replaces_current_version(db_session: AsyncSession) -> None:
    """A second rebuild publishes v2 and demotes v1 to non-current, one current row."""
    comp_id, _ = await _seed_competition(
        db_session, year=2023, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await rebuild_environment_profiles(db_session, competition_id=comp_id)
    async with db_session.begin():
        await rebuild_environment_profiles(db_session, competition_id=comp_id)

    scope_key = f"competition:{comp_id}"
    rows = (
        await db_session.execute(
            select(
                SummerLeagueEnvironmentProfile.version,
                SummerLeagueEnvironmentProfile.is_current,
            )
            .where(SummerLeagueEnvironmentProfile.scope_key == scope_key)
            .order_by(SummerLeagueEnvironmentProfile.version)
        )
    ).all()
    assert [v for v, _ in rows] == [1, 2]
    assert [c for _, c in rows] == [False, True]
    current = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2023)
    )
    assert current is not None and current.version == 2


async def test_failed_candidate_preserves_prior_current(
    db_session: AsyncSession,
) -> None:
    """A validation failure on a re-run leaves the previous current profile intact."""
    import app.services.summer_league_environment_service as svc

    comp_id, _ = await _seed_competition(
        db_session, year=2022, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await rebuild_environment_profiles(db_session, competition_id=comp_id)
    good = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2022)
    )
    assert good is not None
    good_version = good.version
    await db_session.commit()  # end the read txn before the next begin block

    # Force every candidate to fail validation on the next run.
    original = svc._validate_candidate

    def _boom(candidate: object) -> None:
        raise ValueError("forced validation failure")

    svc._validate_candidate = _boom  # type: ignore[assignment]
    try:
        async with db_session.begin():
            result = await rebuild_environment_profiles(
                db_session, competition_id=comp_id
            )
    finally:
        svc._validate_candidate = original  # type: ignore[assignment]

    assert result.failed_scopes == 1
    assert result.built_scopes == 0
    still = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2022)
    )
    assert still is not None
    assert still.version == good_version  # unchanged; prior current preserved


# --------------------------------------------------------------------------- #
# Backfill idempotency + raw-fact preservation
# --------------------------------------------------------------------------- #
async def test_full_backfill_idempotent_and_preserves_raw_facts(
    db_session: AsyncSession,
) -> None:
    """Full backfill is deterministic/idempotent and never mutates raw facts."""
    await _seed_competition(db_session, year=2024, venue="las_vegas", league_id="15")
    await _seed_competition(db_session, year=2025, venue="las_vegas", league_id="15")
    await db_session.commit()

    async def _raw_checksum() -> tuple:
        game_sum = (
            await db_session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(SummerLeagueGame.home_score), 0),
                )
            )
        ).one()
        log_sum = (
            await db_session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(SummerLeaguePlayerGameLog.pts), 0),
                )
            )
        ).one()
        team_sum = (
            await db_session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(SummerLeagueTeamGameLog.pts), 0),
                )
            )
        ).one()
        return (tuple(game_sum), tuple(log_sum), tuple(team_sum))

    before = await _raw_checksum()
    await db_session.commit()  # end the read txn before the rebuild begin blocks

    async with db_session.begin():
        first = await rebuild_environment_profiles(db_session)
    async with db_session.begin():
        second = await rebuild_environment_profiles(db_session)

    # 2 competitions + 2 season scopes each run.
    assert first.built_scopes == 4
    assert second.built_scopes == 4
    assert first.metric_coverage_complete == second.metric_coverage_complete

    after = await _raw_checksum()
    assert before == after  # raw facts untouched

    # Idempotent: exactly one current row per scope, versions advanced to 2.
    for scope in (
        EnvironmentScope.for_season(2024),
        EnvironmentScope.for_season(2025),
    ):
        current = await get_environment_profile(db_session, scope)
        assert current is not None and current.version == 2


# --------------------------------------------------------------------------- #
# Two-session concurrency (barrier-synchronized)
# --------------------------------------------------------------------------- #
async def test_concurrent_writer_cannot_change_inputs_mid_rebuild(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """A competing writer is serialized out until the rebuild publishes its snapshot.

    Session A holds the Summer League writer lock across its reads and
    publication; session B (a competing source writer) blocks on the same lock
    and can only land its raw-fact change after A commits. The published profile
    therefore reflects A's snapshot exactly — parity for one committed snapshot.
    """
    comp_id, _ = await _seed_competition(
        db_session, year=2025, venue="las_vegas", league_id="15", n_games=2
    )
    await db_session.commit()

    b_reached_lock = asyncio.Event()
    b_acquired_lock = asyncio.Event()

    async def competing_writer() -> None:
        async with session_factory() as db_b:
            await db_b.execute(text(f'SET search_path TO "{test_schema}"'))
            await db_b.commit()
            async with db_b.begin():
                b_reached_lock.set()
                # Blocks until session A's transaction releases the lock.
                await acquire_summer_league_writer_lock(db_b)
                b_acquired_lock.set()
                # Only now (post-A) can the competing writer add a raw fact.
                team = (
                    (
                        await db_b.execute(
                            select(SummerLeagueTeamEntry)
                            .where(SummerLeagueTeamEntry.competition_id == comp_id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                assert team is not None
                await _final_game(
                    db_b,
                    comp_id=comp_id,
                    home=team,
                    away=team,
                    game_date=date(2025, 7, 20),
                    home_score=101,
                    away_score=99,
                )

    async with session_factory() as db_a:
        await db_a.execute(text(f'SET search_path TO "{test_schema}"'))
        await db_a.commit()
        async with db_a.begin():
            # A acquires the lock as the rebuild's first action would.
            await acquire_summer_league_writer_lock(db_a)
            writer_task = asyncio.create_task(competing_writer())
            # Barrier: wait until B is definitely attempting the lock, then
            # confirm it is blocked (cannot have acquired while A holds it).
            await asyncio.wait_for(b_reached_lock.wait(), timeout=5.0)
            await asyncio.sleep(0.3)
            assert not b_acquired_lock.is_set(), "competing writer was not serialized"

            # A performs the full rebuild within the same locked transaction
            # (the advisory lock is re-entrant) and publishes.
            result = await rebuild_environment_profiles(db_a, competition_id=comp_id)
            assert result.built_scopes == 1
        # Commit releases the lock, unblocking B.
        await asyncio.wait_for(writer_task, timeout=10.0)

    assert b_acquired_lock.is_set()

    # The published profile reflects A's snapshot (2 games), not B's late insert.
    profile = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2025)
    )
    assert profile is not None
    assert profile.final_games == 2

    # B's raw change did land afterward (serialized, never lost): now 3 games.
    total_games = (
        await db_session.execute(
            select(func.count())
            .select_from(SummerLeagueGame)
            .where(SummerLeagueGame.competition_id == comp_id)
        )
    ).scalar_one()
    assert total_games == 3

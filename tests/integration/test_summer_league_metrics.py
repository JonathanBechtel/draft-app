"""Integration test for the Summer League metrics rebuild.

Seeds a complete, advanced-eligible pool plus a thin pool, runs the full
``rebuild`` against the DB, and asserts the materialized tables, the gating
(eligible vs. not), and the league-relative invariant (PER → 15).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueMetricModel,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.metrics import rebuild
from tests.integration.conftest import make_player

_N = {"i": 0}

# One player's per-game line; six per team gives 180 team minutes (≥150 = complete).
_LINE = dict(
    minutes_seconds=1800,
    pts=12,
    fgm=5,
    fga=10,
    fg3m=1,
    fg3a=3,
    ftm=1,
    fta=2,
    oreb=1,
    dreb=3,
    reb=4,
    ast=2,
    stl=1,
    blk=1,
    tov=2,
    pf=2,
)


async def _team(db: AsyncSession, comp_id: int, idx: int) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"t-{_N['i']}",
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
        team_slug=f"team-{_N['i']}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _players(db: AsyncSession, n: int) -> list:
    out = []
    for _i in range(n):
        _N["i"] += 1
        p = make_player(f"First{_N['i']}", f"Last{_N['i']}")
        db.add(p)
        await db.flush()
        sp = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"sp-{_N['i']}",
            raw_player_name=p.display_name or "P",
            normalized_name=(p.display_name or "p").lower(),
            canonical_player_id=p.id,
        )
        db.add(sp)
        await db.flush()
        out.append((p, sp))
    return out


async def _seed_pool(
    db: AsyncSession,
    *,
    year: int,
    venue: str,
    league_id: str,
    players_per_team: int,
    n_games: int,
) -> int:
    """Seed a two-team pool with ``n_games`` complete games; return competition id."""
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue,
        display_name=f"{year} {venue}",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team_a = await _team(db, comp.id, 1)
    team_b = await _team(db, comp.id, 2)
    roster_a = await _players(db, players_per_team)
    roster_b = await _players(db, players_per_team)

    n = players_per_team
    team_total = {k: v * n for k, v in _LINE.items() if k != "minutes_seconds"}
    team_minutes = (_LINE["minutes_seconds"] // 60) * n

    for g in range(n_games):
        _N["i"] += 1
        home, away = (team_a, team_b) if g % 2 == 0 else (team_b, team_a)
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"g-{_N['i']}",
            game_date=date(year, 7, 6),
            home_team_entry_id=home.id,
            away_team_entry_id=away.id,
            home_score=80,
            away_score=72,
        )
        db.add(game)
        await db.flush()
        for team, roster in ((team_a, roster_a), (team_b, roster_b)):
            db.add(
                SummerLeagueTeamGameLog(
                    competition_id=comp.id,
                    game_id=game.id,
                    team_entry_id=team.id,
                    minutes=team_minutes,
                    **team_total,
                )
            )
            for pid, (player, sp) in enumerate(roster):
                db.add(
                    SummerLeaguePlayerGameLog(
                        competition_id=comp.id,
                        game_id=game.id,
                        team_entry_id=team.id,
                        source_player_id=sp.id,
                        player_id=player.id,
                        nba_stats_person_id=sp.nba_stats_person_id,
                        raw_player_name=player.display_name or "P",
                        plus_minus=(2 if pid % 2 == 0 else -2),
                        **_LINE,
                    )
                )
    await db.flush()
    return comp.id


@pytest.mark.asyncio
async def test_rebuild_materializes_metrics_and_gates_pools(
    db_session: AsyncSession,
) -> None:
    """Rebuild populates all three tables; eligible pool gets composites, thin doesn't."""
    # Eligible: 12 players, 4 complete games. Thin: 3 players, 1 game.
    elig_id = await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    thin_id = await _seed_pool(
        db_session,
        year=2025,
        venue="orlando",
        league_id="14",
        players_per_team=3,
        n_games=1,
    )
    await db_session.commit()

    async with db_session.begin():
        summary = await rebuild(db_session)

    assert summary["contexts"] == 2
    assert summary["adv_pools"] == 1
    assert summary["seasons"] == 12 + 6  # eligible 12 + thin 6

    # Exactly one model row, with sane fit metadata.
    model = (await db_session.execute(select(SummerLeagueMetricModel))).scalars().one()
    assert model.pyth_exponent > 0
    assert model.ws_ppw_coeff == pytest.approx(4.0 / model.pyth_exponent)

    # Context gating.
    ctxs = {
        c.competition_id: c
        for c in (await db_session.execute(select(SummerLeagueMetricContext))).scalars()
    }
    assert ctxs[elig_id].adv_eligible is True
    assert ctxs[thin_id].adv_eligible is False

    # Eligible pool: composites present, PER standardized to ~15 (uniform lines).
    elig = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == elig_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(elig) == 12
    assert all(s.adv_eligible for s in elig)
    assert all(s.gmsc is not None for s in elig)
    assert all(s.per is not None and abs(s.per - 15.0) < 0.5 for s in elig)
    assert all(s.ortg is not None and s.ws is not None for s in elig)
    # WS exposes both flavours: a cumulative total and its 82-game projection.
    # (VORP rides on BPM, whose regression is degenerate on this uniform pool, so
    # bpm/vorp are null here — the VORP formulas are checked in the unit suite.)
    assert all(s.ws82 is not None for s in elig)
    assert all(abs(s.ws) <= abs(s.ws82) for s in elig)  # few games << a season

    # Thin pool: box/shooting only; league-relative composites blanked.
    thin = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == thin_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(thin) == 6
    assert all(not s.adv_eligible for s in thin)
    assert all(s.gmsc is not None for s in thin)  # box-derived still computed
    # pace / pts_per100 are raw possession measures, populated even for thin pools
    # so per-100 works outside adv_eligible pools (issue #473).
    assert all(s.pace is not None for s in thin)
    assert all(s.pts_per100 is not None for s in thin)
    # League-relative / pool-calibrated composites remain blanked.
    assert all(s.per is None and s.ortg is None for s in thin)
    assert all(s.ws82 is None and s.vorp82 is None for s in thin)


@pytest.mark.asyncio
async def test_rebuild_is_idempotent(db_session: AsyncSession) -> None:
    """Running the rebuild twice replaces rows rather than accumulating them."""
    await _seed_pool(
        db_session,
        year=2024,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    async with db_session.begin():
        await rebuild(db_session)
    async with db_session.begin():
        summary = await rebuild(db_session)

    total = (await db_session.execute(select(SummerLeaguePlayerSeason))).scalars().all()
    assert len(total) == summary["seasons"] == 12
    models = (await db_session.execute(select(SummerLeagueMetricModel))).scalars().all()
    assert len(models) == 1  # not duplicated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "excluded_status",
    [
        SummerLeagueGameStatus.IN_PROGRESS,
        SummerLeagueGameStatus.POSTPONED,
        SummerLeagueGameStatus.CANCELED,
    ],
)
async def test_rebuild_excludes_non_season_game_until_it_is_final(
    db_session: AsyncSession,
    excluded_status: SummerLeagueGameStatus,
) -> None:
    """Season metrics retain only games that are eligible for season totals.

    Historical games with the default UNKNOWN status remain in the pool. A
    complete-looking game that is live, postponed, or canceled must not change
    Tier-2 totals until its status advances to FINAL.
    """
    competition_id = await _seed_pool(
        db_session,
        year=2026,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    base_game = (
        await db_session.execute(
            select(SummerLeagueGame)
            .where(SummerLeagueGame.competition_id == competition_id)  # type: ignore[arg-type]
            .order_by(SummerLeagueGame.id)
            .limit(1)
        )
    ).scalar_one()
    assert base_game.id is not None

    live_game = SummerLeagueGame(
        competition_id=competition_id,
        nba_stats_game_id="live-game-excluded-from-season",
        game_date=date(2026, 7, 10),
        home_team_entry_id=base_game.home_team_entry_id,
        away_team_entry_id=base_game.away_team_entry_id,
        status=excluded_status,
    )
    db_session.add(live_game)
    await db_session.flush()
    assert live_game.id is not None

    base_team_logs = (
        (
            await db_session.execute(
                select(SummerLeagueTeamGameLog).where(
                    SummerLeagueTeamGameLog.game_id == base_game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    base_player_logs = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerGameLog).where(
                    SummerLeaguePlayerGameLog.game_id == base_game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(base_team_logs) == 2
    assert len(base_player_logs) == 12

    for team_log in base_team_logs:
        db_session.add(
            SummerLeagueTeamGameLog(
                competition_id=competition_id,
                game_id=live_game.id,
                team_entry_id=team_log.team_entry_id,
                minutes=team_log.minutes,
                **{
                    field: getattr(team_log, field)
                    for field in _LINE
                    if field != "minutes_seconds"
                },
            )
        )
    for player_log in base_player_logs:
        db_session.add(
            SummerLeaguePlayerGameLog(
                competition_id=competition_id,
                game_id=live_game.id,
                team_entry_id=player_log.team_entry_id,
                source_player_id=player_log.source_player_id,
                player_id=player_log.player_id,
                nba_stats_person_id=player_log.nba_stats_person_id,
                raw_player_name=player_log.raw_player_name,
                plus_minus=player_log.plus_minus,
                **_LINE,
            )
        )
    await db_session.flush()

    await rebuild(db_session)
    initial_seasons = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == competition_id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(initial_seasons) == 12
    assert {season.gp for season in initial_seasons} == {4}
    assert all(season.adv_eligible for season in initial_seasons)

    live_game.status = SummerLeagueGameStatus.FINAL
    await db_session.flush()
    await rebuild(db_session)
    final_seasons = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == competition_id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert {season.gp for season in final_seasons} == {5}

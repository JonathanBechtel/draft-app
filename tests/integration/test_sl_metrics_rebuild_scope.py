"""Integration tests for the scoped Summer League metrics rebuild (#523).

`app.services.summer_league.metrics.rebuild` appends an inactive dated
projection and flips the selected scopes current after the build. That keeps
history for the offline job while making the hourly desk tick safe to refresh
only the competition(s) it normalized this hour.

These tests prove, in two groups:

* **Direct `rebuild()` scoping** -- a scoped call (`competition_ids=[...]`)
  refreshes only the target competition's `SummerLeaguePlayerSeason` /
  `SummerLeagueMetricContext` rows; a different competition's rows survive
  byte-for-byte, the shared `SummerLeagueMetricModel` row is left alone, and
  re-running the scoped call is idempotent. A parallel unscoped-call test is
  the regression guard for the pre-#523 full wipe-and-rebuild behavior.
* **`run_desk_tick` wiring** -- a tick over freshly-normalized raw box logs
  calls the scoped rebuild between normalize and grades, so
  `grade_player_event` ranks a player against a freshly recomputed
  aggregate rather than a stale one, while a competition/year the tick
  never touches (including a row `rebuild` never wrote) is left alone; the
  off-window/dormant tick remains fully inert.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueMetricModel,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league import metrics
from app.services.summer_league.metrics import game_score_line, rebuild
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.stats.registry import METRIC_REGISTRY_VERSION
from app.services.summer_league.metric_publish import publish_metric_version
from app.cli.sl_desk_tick import run_desk_tick
from tests.integration.conftest import make_player

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


# --------------------------------------------------------------------------- #
# Group A -- direct `rebuild()` scoping (no raw JSON / normalize involved).
# --------------------------------------------------------------------------- #
# One player's per-game line; six per team gives 180 team minutes (>=150 = complete).
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
    i = _next_idx()
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"scope-t-{i}",
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
        team_slug=f"scope-team-{i}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _players(db: AsyncSession, n: int) -> list[tuple[PlayerMaster, Any]]:
    out = []
    for _i in range(n):
        i = _next_idx()
        p = make_player(f"ScopeFirst{i}", f"ScopeLast{i}")
        db.add(p)
        await db.flush()
        sp = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"scope-sp-{i}",
            raw_player_name=p.display_name or "P",
            normalized_name=(p.display_name or "p").lower(),
            canonical_player_id=p.id,
        )
        db.add(sp)
        await db.flush()
        out.append((p, sp))
    return out


async def _seed_pool(  # noqa: PLR0913 - fixture parameters describe the seeded pool
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
        i = _next_idx()
        home, away = (team_a, team_b) if g % 2 == 0 else (team_b, team_a)
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"scope-g-{i}",
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


async def _distinct_roster(
    db: AsyncSession, *, team_entry_id: int
) -> list[SummerLeaguePlayerGameLog]:
    """One representative logged row per distinct player already on this team."""
    rows = (
        (
            await db.execute(
                select(SummerLeaguePlayerGameLog).where(
                    SummerLeaguePlayerGameLog.team_entry_id == team_entry_id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    seen: set[int] = set()
    out: list[SummerLeaguePlayerGameLog] = []
    for row in rows:
        if row.player_id in seen:
            continue
        seen.add(row.player_id)  # type: ignore[arg-type]
        out.append(row)
    return out


async def _add_game(
    db: AsyncSession,
    *,
    comp_id: int,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
) -> None:
    """Append one more complete game to an already-seeded pool (simulates new data).

    Every player already rostered on either team picks up one more logged
    game (so every player-season's ``gp`` increments uniformly), keeping the
    pool internally consistent for the rebuild's league-context math.
    """
    i = _next_idx()
    game = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"scope-g-extra-{i}",
        game_date=date(2025, 7, 6),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=80,
        away_score=72,
    )
    db.add(game)
    await db.flush()

    assert home.id is not None and away.id is not None
    home_id: int = home.id
    away_id: int = away.id
    rosters = {
        home_id: await _distinct_roster(db, team_entry_id=home_id),
        away_id: await _distinct_roster(db, team_entry_id=away_id),
    }
    for team_id in (home_id, away_id):
        n = len(rosters[team_id])
        team_total = {k: v * n for k, v in _LINE.items() if k != "minutes_seconds"}
        team_minutes = (_LINE["minutes_seconds"] // 60) * n
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp_id,
                game_id=game.id,
                team_entry_id=team_id,
                minutes=team_minutes,
                **team_total,
            )
        )
        for row in rosters[team_id]:
            db.add(
                SummerLeaguePlayerGameLog(
                    competition_id=comp_id,
                    game_id=game.id,
                    team_entry_id=team_id,
                    source_player_id=row.source_player_id,
                    player_id=row.player_id,
                    nba_stats_person_id=row.nba_stats_person_id,
                    raw_player_name=row.raw_player_name,
                    plus_minus=3,
                    **_LINE,
                )
            )
    await db.flush()


async def test_scoped_rebuild_refreshes_target_only_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    """A scoped rebuild refreshes only its competition.

    The other competition's rows -- and the shared model row -- are left
    exactly as the prior full rebuild wrote them.
    """
    comp_a = await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    comp_b = await _seed_pool(
        db_session,
        year=2025,
        venue="orlando",
        league_id="14",
        players_per_team=6,
        n_games=4,
    )
    home_a = (
        (
            await db_session.execute(
                select(SummerLeagueTeamEntry).where(
                    SummerLeagueTeamEntry.competition_id == comp_a  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .first()
    )
    away_a = (
        (
            await db_session.execute(
                select(SummerLeagueTeamEntry).where(
                    SummerLeagueTeamEntry.competition_id == comp_a  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )[1]
    await db_session.commit()

    # Establish the baseline: one full (unscoped) rebuild over both pools.
    summary = await rebuild(db_session)
    await db_session.commit()
    assert summary["seasons"] == 12 + 12
    model_before = (
        (await db_session.execute(select(SummerLeagueMetricModel))).scalars().one()
    )

    b_seasons_before = {
        s.id: (s.player_id, s.gmsc, s.minutes, s.gp)
        for s in (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == comp_b,  # type: ignore[arg-type]
                    SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    }
    b_context_before = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext).where(
                    SummerLeagueMetricContext.competition_id == comp_b,  # type: ignore[arg-type]
                    SummerLeagueMetricContext.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .one()
    )

    # New data lands for A only (mirrors "normalize picked up a new game this
    # hour") -- append an extra game, then scope the rebuild to A.
    assert home_a is not None and home_a.id is not None
    assert away_a.id is not None
    await _add_game(db_session, comp_id=comp_a, home=home_a, away=away_a)
    await db_session.commit()

    scoped_summary = await rebuild(db_session, competition_ids=[comp_a])
    await db_session.commit()
    assert scoped_summary["contexts"] == 1
    assert scoped_summary["seasons"] == 12  # still 12 distinct players in A

    # A actually changed: gp went from 4 to 5 for every player.
    a_seasons_after = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == comp_a,  # type: ignore[arg-type]
                    SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(a_seasons_after) == 12
    assert all(s.gp == 5 for s in a_seasons_after)
    assert all(
        season.trend_season_bands and "gmsc" in season.trend_season_bands
        for season in a_seasons_after
    )

    # B is untouched: same row ids, same values, same context row.
    b_seasons_after = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == comp_b,  # type: ignore[arg-type]
                    SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert {
        s.id: (s.player_id, s.gmsc, s.minutes, s.gp) for s in b_seasons_after
    } == b_seasons_before
    b_context_after = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext).where(
                    SummerLeagueMetricContext.competition_id == comp_b,  # type: ignore[arg-type]
                    SummerLeagueMetricContext.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .one()
    )
    assert b_context_after.id == b_context_before.id
    assert b_context_after.pace == b_context_before.pace

    # The shared model row is untouched by the scoped call -- still exactly
    # the one the earlier full rebuild wrote.
    models_after_scoped = (
        (await db_session.execute(select(SummerLeagueMetricModel))).scalars().all()
    )
    assert len(models_after_scoped) == 1
    assert models_after_scoped[0].id == model_before.id
    assert models_after_scoped[0].model_version == model_before.model_version

    # Idempotency: scoping the rebuild to A again with no new data doesn't
    # duplicate rows or perturb values.
    second_scoped_summary = await rebuild(db_session, competition_ids=[comp_a])
    await db_session.commit()
    assert second_scoped_summary["seasons"] == 12
    a_seasons_twice = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == comp_a,  # type: ignore[arg-type]
                    SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(a_seasons_twice) == 12
    assert {s.player_id: s.gp for s in a_seasons_twice} == {
        s.player_id: 5 for s in a_seasons_twice
    }


async def test_metric_publish_stamps_source_watermark_and_hides_candidates(
    db_session: AsyncSession,
) -> None:
    """Candidates stay invisible until publication stamps their source currency."""
    comp_id = await _seed_pool(
        db_session,
        year=2026,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    staged = await metrics.rebuild_staged(db_session, model_version="watermark-fit")
    watermark = staged["as_of"]
    assert watermark is not None
    version = staged["version"]
    await db_session.commit()

    staged_contexts = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext).where(
                    SummerLeagueMetricContext.competition_id == comp_id,
                    SummerLeagueMetricContext.version == version,
                )
            )
        )
        .scalars()
        .all()
    )
    staged_seasons = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == comp_id,
                    SummerLeaguePlayerSeason.version == version,
                )
            )
        )
        .scalars()
        .all()
    )
    assert staged_contexts and staged_seasons
    assert all(row.is_current is False for row in [*staged_contexts, *staged_seasons])
    await db_session.commit()

    async with db_session.begin():
        await publish_metric_version(
            db_session,
            version=version,
            model_version="watermark-fit",
            as_of=watermark,
        )

    current_context = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext).where(
                    SummerLeagueMetricContext.competition_id == comp_id,
                    SummerLeagueMetricContext.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .one()
    )
    current_seasons = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == comp_id,
                    SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert current_context.as_of == watermark
    assert current_seasons and all(row.as_of == watermark for row in current_seasons)
    # Published rows carry the stat-engine registry version they were built
    # under, so a registry bump is auditable from the projection itself rather
    # than only from the rebuild summary.
    assert current_context.registry_version == METRIC_REGISTRY_VERSION
    assert all(
        row.registry_version == METRIC_REGISTRY_VERSION for row in current_seasons
    )


def _scoped_projection(result: metrics.ComputeResult, competition_id: int) -> tuple:
    """Return the comparable projection portion for one competition."""
    seasons = sorted(
        (
            season.player_id,
            season,
        )
        for season in result.seasons
        if season.competition_id == competition_id
    )
    return result.contexts[competition_id], seasons


async def test_scoped_compute_reuses_fit_and_matches_full_recompute(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped projection equals a full recompute without refitting the pool."""
    comp_a = await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await _seed_pool(
        db_session,
        year=2025,
        venue="orlando",
        league_id="14",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    # Publish the full fit first, just as the first offline rebuild or a prior
    # event tick would. The next scoped call may now avoid loading competition B.
    await metrics.rebuild(db_session)
    await db_session.commit()
    full = await metrics.compute(db_session)

    def fail_fit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scoped compute unexpectedly refit the full pool")

    monkeypatch.setattr(metrics, "fit_pythagorean", fail_fit)
    monkeypatch.setattr(metrics, "fit_sl_bpm", fail_fit)
    scoped = await metrics.compute(db_session, competition_ids=[comp_a])

    assert _scoped_projection(full, comp_a) == _scoped_projection(scoped, comp_a)

    # This is an executable fail-case for the parity assertion: a changed
    # metric cannot hide behind a self-consistent scoped result.
    scoped.seasons[0].metrics["ts_pct"] = (
        scoped.seasons[0].metrics["ts_pct"] or 0.0
    ) + 1.0
    with pytest.raises(AssertionError):
        assert _scoped_projection(full, comp_a) == _scoped_projection(scoped, comp_a)


async def test_historical_scoped_compute_refits_only_through_cutoff(
    db_session: AsyncSession,
) -> None:
    """A historical close cannot reuse a fit trained on later-season games."""
    historical_comp = await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="historical-fit",
        players_per_team=6,
        n_games=4,
    )
    peer_comp = await _seed_pool(
        db_session,
        year=2025,
        venue="salt_lake_city",
        league_id="historical-peer-fit",
        players_per_team=6,
        n_games=4,
    )
    await _seed_pool(
        db_session,
        year=2026,
        venue="orlando",
        league_id="future-fit",
        players_per_team=6,
        n_games=4,
    )
    peer_game = (
        (
            await db_session.execute(
                select(SummerLeagueGame).where(
                    SummerLeagueGame.competition_id == peer_comp
                )
            )
        )
        .scalars()
        .first()
    )
    assert peer_game is not None
    peer_watermark = datetime(2030, 1, 1, 12)
    peer_game.updated_at = peer_watermark
    await db_session.commit()

    await metrics.rebuild(db_session)
    await db_session.commit()
    active_fit = await metrics._load_active_fit(db_session)
    assert active_fit is not None
    assert active_fit.model_version is not None
    assert active_fit.bpm_n_fit == 36

    historical = await metrics.compute(
        db_session,
        competition_ids=[historical_comp],
        through_day=date(2025, 7, 6),
    )

    assert historical.fit.model_version is None
    assert historical.fit.bpm_n_fit == 24
    assert historical.as_of == peer_watermark
    assert {season.competition_id for season in historical.seasons} == {historical_comp}


async def test_scoped_rebuild_empty_scope_is_a_noop(db_session: AsyncSession) -> None:
    """An empty ``competition_ids`` sequence changes nothing.

    Mirrors "nothing new to normalize this tick" from the desk tick's own
    normalize step.
    """
    comp_a = await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    await rebuild(db_session)
    await db_session.commit()
    seasons_before = (
        (await db_session.execute(select(SummerLeaguePlayerSeason))).scalars().all()
    )
    assert len(seasons_before) == 12

    summary = await rebuild(db_session, competition_ids=[])
    await db_session.commit()
    assert summary["seasons"] == 0
    assert summary["contexts"] == 0
    assert summary["adv_pools"] == 0
    assert summary["version"] == 0

    seasons_after = (
        (await db_session.execute(select(SummerLeaguePlayerSeason))).scalars().all()
    )
    assert {s.id for s in seasons_after} == {s.id for s in seasons_before}
    assert comp_a  # sanity: the pool used above


async def test_unscoped_rebuild_retains_projection_history(
    db_session: AsyncSession,
) -> None:
    """An unscoped rebuild retains prior versions while publishing one current view."""
    await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await _seed_pool(
        db_session,
        year=2025,
        venue="orlando",
        league_id="14",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    async with db_session.begin():
        first = await rebuild(db_session, model_version="fit-1")
    async with db_session.begin():
        second = await rebuild(db_session, model_version="fit-2")

    assert first["seasons"] == second["seasons"] == 24
    assert first["contexts"] == second["contexts"] == 2

    seasons = (
        (await db_session.execute(select(SummerLeaguePlayerSeason))).scalars().all()
    )
    current_seasons = [season for season in seasons if season.is_current]
    assert len(seasons) == 48
    assert len(current_seasons) == 24
    assert {season.effective_day for season in current_seasons} == {date(2025, 7, 6)}
    assert first["effective_day"] is None
    assert second["effective_day"] is None

    # Projections are replaced; fits accumulate. Each unscoped rebuild retains the
    # prior model row and deactivates it rather than deleting it (P2).
    models = (await db_session.execute(select(SummerLeagueMetricModel))).scalars().all()
    assert {m.model_version for m in models} == {"fit-1", "fit-2"}
    assert [m.model_version for m in models if m.is_active] == ["fit-2"]


# --------------------------------------------------------------------------- #
# Group B -- `run_desk_tick` wiring: the scoped rebuild fires between normalize
# and grades, over real raw JSON normalized within the tick itself.
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Minimal response object mirroring the curl_cffi shape the client reads."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured JSON payload."""
        return self.payload


class FakeSession:
    """Fake curl_cffi-compatible session that never touches the network."""

    def __init__(self, responses_by_league: dict[str, FakeResponse]) -> None:
        self.responses_by_league = responses_by_league

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        """Return the response registered for the request's LeagueID."""
        league_id = params.get("LeagueID", "")
        if league_id not in self.responses_by_league:
            return FakeResponse({}, status_code=404)
        return self.responses_by_league[league_id]

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


def _empty_schedule_payload() -> dict[str, Any]:
    return {"leagueSchedule": {"gameDates": []}}


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def _write_raw_fixture(raw_root: Path, *, year: int) -> None:
    """One game, two teams, one resolvable rostered player with a real box line.

    Adapted from the already-proven `tests/integration/test_summer_league_player_log_normalization.py`
    fixture -- same shapes, same endpoints, changed only to make the box
    player ("1640001") the one with an actual box line (rather than the DNP
    row that fixture used for its "resolved" player) so it feeds a real
    `gmsc`.
    """
    run_dir = raw_root / str(year) / "15"
    game_dir = run_dir / "games" / "1522600001"
    game_dir.mkdir(parents=True)
    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": year,
                "league_id": "15",
                "venue": "las_vegas",
                "team_gamelog_rows": 2,
                "player_gamelog_rows": 1,
                "game_ids": ["1522600001"],
                "game_count": 1,
                "errors": [],
            }
        )
    )
    run_dir.joinpath("leaguegamelog_team.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        [
                            "TEAM_ID",
                            "TEAM_ABBREVIATION",
                            "TEAM_NAME",
                            "GAME_ID",
                            "GAME_DATE",
                            "MATCHUP",
                            "PTS",
                        ],
                        [
                            [
                                1610612753,
                                "ORL",
                                "Orlando Magic",
                                "1522600001",
                                f"{year}-07-10",
                                "ORL vs. CLE",
                                106,
                            ],
                            [
                                1610612739,
                                "CLE",
                                "Cleveland Cavaliers",
                                "1522600001",
                                f"{year}-07-10",
                                "CLE @ ORL",
                                79,
                            ],
                        ],
                    )
                ]
            }
        )
    )
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID"],
                        [[1640001, "Rookie Guy", 1610612753]],
                    )
                ]
            }
        )
    )
    game_dir.joinpath("boxscoretraditionalv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayerStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "PLAYER_ID",
                            "PLAYER_NAME",
                            "START_POSITION",
                            "COMMENT",
                            "MIN",
                            "FGM",
                            "FGA",
                            "FG_PCT",
                            "FG3M",
                            "FG3A",
                            "FG3_PCT",
                            "FTM",
                            "FTA",
                            "FT_PCT",
                            "OREB",
                            "DREB",
                            "REB",
                            "AST",
                            "STL",
                            "BLK",
                            "TO",
                            "PF",
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                "1522600001",
                                1610612753,
                                1640001,
                                "Rookie Guy",
                                "G",
                                "",
                                "24:28",
                                6,
                                11,
                                0.545,
                                2,
                                4,
                                0.5,
                                3,
                                4,
                                0.75,
                                1,
                                3,
                                4,
                                5,
                                2,
                                1,
                                3,
                                2,
                                17,
                                8,
                            ]
                        ],
                    ),
                    _result_set(
                        "TeamStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "TEAM_NAME",
                            "TEAM_ABBREVIATION",
                            "MIN",
                            "PTS",
                        ],
                        [
                            ["1522600001", 1610612753, "Magic", "ORL", "200:00", 106],
                            [
                                "1522600001",
                                1610612739,
                                "Cavaliers",
                                "CLE",
                                "200:00",
                                79,
                            ],
                        ],
                    ),
                ]
            }
        )
    )
    game_dir.joinpath("boxscoreadvancedv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("PlayerStats", [], []),
                    _result_set("TeamStats", [], []),
                ]
            }
        )
    )
    game_dir.joinpath("boxscorescoringv2.json").write_text(
        json.dumps({"resultSets": []})
    )
    game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))
    game_dir.joinpath("shotchartdetail.json").write_text(json.dumps({"resultSets": []}))


async def _seed_state_unlocking_game(
    db: AsyncSession, *, year: int, league_id: str, game_date: date, tip: datetime
) -> SummerLeagueCompetition:
    """Pre-seed a competition + FINAL game so the tick resolves an active state.

    The Job B dormancy pre-check runs *before* normalize can create
    anything, so ``game_date`` needs a game that already exists.
    """
    i = _next_idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"vegas-tick-{i}",
        display_name=f"{year} vegas",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    home = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"tick-t-{i}-h",
        raw_team_name="Home",
        team_slug=f"tick-home-{i}",
    )
    away = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"tick-t-{i}-a",
        raw_team_name="Away",
        team_slug=f"tick-away-{i}",
    )
    db.add(home)
    db.add(away)
    await db.flush()
    assert home.id is not None and away.id is not None
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"tick-unlock-{i}",
        game_date=game_date,
        tip_datetime=tip,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=SummerLeagueGameStatus.FINAL,
    )
    db.add(game)
    await db.flush()
    return comp


async def _seed_baseline(db: AsyncSession, *, baseline_version: str) -> None:
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key="slot:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.EVENT,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=40.0,
            n_members=20,
            breakpoints={"0": 10.0, "25": 30.0, "50": 50.0, "75": 70.0, "100": 90.0},
            mean_value=50.0,
            median_value=50.0,
        )
    )
    await db.flush()


async def test_desk_tick_scoped_rebuild_refreshes_normalized_competition_only(  # noqa: PLR0915 - end-to-end tick assertions
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A tick refreshes only the competition normalize just touched this hour.

    A different competition/year's row, never touched by normalize this
    tick, survives untouched, and `grade_player_event` reads the freshly
    rebuilt value rather than a stale one seeded before the tick ran.
    """
    year = 2026
    now = datetime(year, 7, 10, 20, 0)

    # Competition A: unlocks the active daily state, then gets normalized
    # from the raw fixture during the tick itself (same year/league_id, so
    # normalize's upsert reuses this same competition row).
    competition_a = await _seed_state_unlocking_game(
        db_session,
        year=year,
        league_id="15",
        game_date=date(year, 7, 10),
        tip=datetime(year, 7, 10, 18, 0),
    )
    assert competition_a.id is not None

    player = PlayerMaster(
        first_name="Rookie",
        last_name="Guy",
        display_name="Rookie Guy",
        draft_year=year,
        draft_round=1,
        draft_pick=1,
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    db_session.add(
        SummerLeagueSourcePlayer(
            nba_stats_person_id="1640001",
            raw_player_name="Rookie Guy",
            normalized_name="rookie guy",
            canonical_player_id=player.id,
        )
    )

    # A deliberately stale SummerLeaguePlayerSeason row for A -- what a prior
    # tick (or a since-superseded rebuild) left behind. The scoped rebuild
    # this tick performs must replace it with freshly computed values.
    stale_season = SummerLeaguePlayerSeason(
        competition_id=competition_a.id,
        player_id=player.id,
        year=year,
        venue_slug="vegas-tick-stale",
        is_current=True,
        gp=99,
        minutes=1.0,
        gmsc=-999.0,
    )
    db_session.add(stale_season)

    baseline_version = "sl-523-scope-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)

    # Competition B: a different YEAR, so `resolve_target_competitions`
    # excludes it entirely -- this tick never normalizes, grades, or
    # rebuilds anything for it. Its season row must come out byte-for-byte
    # identical to how it went in.
    competition_b = SummerLeagueCompetition(
        year=year - 1,
        league_id="15",
        venue_slug="vegas-untouched",
        display_name=f"{year - 1} vegas",
    )
    db_session.add(competition_b)
    await db_session.flush()
    assert competition_b.id is not None
    other_player = PlayerMaster(
        first_name="Other",
        last_name="Guy",
        display_name="Other Guy",
        draft_year=year - 1,
        is_stub=False,
    )
    db_session.add(other_player)
    await db_session.flush()
    assert other_player.id is not None
    untouched_season = SummerLeaguePlayerSeason(
        competition_id=competition_b.id,
        player_id=other_player.id,
        year=year - 1,
        venue_slug="vegas-untouched",
        is_current=True,
        gp=7,
        minutes=123.0,
        gmsc=55.5,
    )
    db_session.add(untouched_season)
    await db_session.flush()
    untouched_season_id = untouched_season.id

    await db_session.commit()

    _write_raw_fixture(tmp_path, year=year)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=year, league_id="15"
    )
    await db_session.commit()

    session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    client = NBAStatsClient(session=session)

    result = await run_desk_tick(db_session, now=now, client=client, raw_root=tmp_path)
    await db_session.commit()

    assert result.dormant is False
    assert competition_a.id in result.normalized_competition_ids

    # A's season row is refreshed: no longer the stale seeded values, and
    # matches what the box line actually computes to.
    refreshed = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.competition_id == competition_a.id,  # type: ignore[arg-type]
                SummerLeaguePlayerSeason.player_id == player.id,  # type: ignore[arg-type]
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
        )
    ).scalar_one()
    assert refreshed.gp == 1
    # SummerLeaguePlayerSeason.minutes is rounded to 1 dp (24:28 == 24.4667 -> 24.5).
    assert refreshed.minutes == pytest.approx(24.5)
    expected_gmsc = round(
        game_score_line(
            pts=17,
            fgm=6,
            fga=11,
            ftm=3,
            fta=4,
            oreb=1,
            dreb=3,
            ast=5,
            stl=2,
            blk=1,
            tov=3,
            pf=2,
        ),
        1,
    )
    assert refreshed.gmsc == pytest.approx(expected_gmsc)
    assert refreshed.gmsc != -999.0

    # Grading read the freshly rebuilt aggregate, not the stale one.
    assert result.graded_player_ids == (player.id,)
    grade_row = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == player.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == competition_a.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert grade_row.subject_value == pytest.approx(refreshed.gmsc)

    # B's season row -- a different year the tick never touches -- survives
    # byte-for-byte, same id and same (deliberately implausible) values.
    still_untouched = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.competition_id == competition_b.id,  # type: ignore[arg-type]
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
        )
    ).scalar_one()
    assert still_untouched.id == untouched_season_id
    assert still_untouched.gp == 7
    assert still_untouched.minutes == pytest.approx(123.0)
    assert still_untouched.gmsc == pytest.approx(55.5)

    # Re-running the tick over the same (now-normalized) data is idempotent:
    # still exactly one season row for A's player, no duplication.
    second_session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    second_client = NBAStatsClient(session=second_session)
    await run_desk_tick(db_session, now=now, client=second_client, raw_root=tmp_path)
    await db_session.commit()

    a_rows_after_second = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == competition_a.id,  # type: ignore[arg-type]
                    SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(a_rows_after_second) == 1


async def test_desk_tick_off_window_never_touches_player_seasons(
    db_session: AsyncSession,
) -> None:
    """The dormant/off-window path never calls the scoped rebuild.

    A directly seeded `SummerLeaguePlayerSeason` row survives an off-window
    tick untouched -- the rebuild call lives strictly inside the
    active-path branch, same as steps 0-4.
    """
    year = 2099
    now = datetime(year, 1, 15, 12, 0)

    comp = SummerLeagueCompetition(
        year=year, league_id="15", venue_slug="off-window", display_name="off-window"
    )
    db_session.add(comp)
    await db_session.flush()
    assert comp.id is not None
    player = make_player("Dormant", "Player")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    season = SummerLeaguePlayerSeason(
        competition_id=comp.id,
        player_id=player.id,
        year=year,
        venue_slug="off-window",
        is_current=True,
        gp=3,
        minutes=42.0,
        gmsc=12.3,
    )
    db_session.add(season)
    await db_session.commit()
    season_id = season.id

    # No fake NBA client injected: if the dormancy guard regressed, the
    # scoreboard ingest step (or a rebuild call reached in error) would try
    # real work and this test would fail/hang rather than pass silently.
    result = await run_desk_tick(db_session, now=now)
    await db_session.commit()

    assert result.dormant is True

    unchanged = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.id == season_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert unchanged.gp == 3
    assert unchanged.minutes == pytest.approx(42.0)
    assert unchanged.gmsc == pytest.approx(12.3)


# --------------------------------------------------------------------------- #
# Group C -- fit history retention (P2). The model table records *how* the
# numbers were derived; wiping it made each hour's fit unreproducible the
# moment the next hour ran.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repeated_rebuilds_retain_every_fit_with_one_active(db_session):
    """Each unscoped rebuild adds a fit and deactivates the previous one.

    The read path selects ``WHERE is_active IS TRUE ORDER BY id DESC``, so "exactly one
    active" is the invariant that keeps it unambiguous once rows accumulate.
    """
    await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    for version in ("v1", "v2", "v3"):
        async with db_session.begin():
            await rebuild(db_session, model_version=version)

    models = (await db_session.execute(select(SummerLeagueMetricModel))).scalars().all()
    assert {m.model_version for m in models} == {"v1", "v2", "v3"}
    assert [m.model_version for m in models if m.is_active] == ["v3"]


@pytest.mark.asyncio
async def test_rerunning_the_same_version_refits_in_place(db_session):
    """A rebuild is safely re-runnable — a Phase 1 exit criterion.

    ``model_version`` is UNIQUE, so re-publishing an existing version has to refit that
    row rather than raise. This also covers the real collision case: the auto-minted
    version is second-granularity, so two rebuilds inside one second *are* the same
    version and must collapse rather than crash the tick.
    """
    await _seed_pool(
        db_session,
        year=2025,
        venue="las_vegas",
        league_id="15",
        players_per_team=6,
        n_games=4,
    )
    await db_session.commit()

    async with db_session.begin():
        await rebuild(db_session, model_version="same")
    async with db_session.begin():
        await rebuild(db_session, model_version="same")

    models = (await db_session.execute(select(SummerLeagueMetricModel))).scalars().all()
    assert len(models) == 1
    assert models[0].model_version == "same"
    assert models[0].is_active is True


@pytest.mark.asyncio
async def test_database_rejects_a_second_active_fit(db_session):
    """The single-active invariant is enforced by the database, not by callers.

    Publication deactivates prior fits and then writes the new one, which is sound
    inside one transaction -- but the hourly ingestion holds the Summer League writer
    lock while ``scripts/rebuild_sl_metrics.py`` takes no lock at all, so two overlapping
    unscoped rebuilds are not serialized against each other. Without the partial unique
    index, that race leaves two active rows and ``_active_or_fresh_model_version()``
    picks one arbitrarily by id.
    """
    from sqlalchemy.exc import IntegrityError

    def _fit(version: str) -> SummerLeagueMetricModel:
        return SummerLeagueMetricModel(
            model_version=version,
            pyth_exponent=10.0,
            ws_ppw_coeff=0.4,
            pyth_n_teams=1,
            bpm_intercept=0.0,
            bpm_r2=0.5,
            bpm_n_fit=10,
            bpm_replacement=-2.0,
            bpm_coefficients={},
            is_active=True,
        )

    db_session.add(_fit("race-a"))
    await db_session.commit()

    db_session.add(_fit("race-b"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

"""Integration tests for wiring all eight #520 fact detectors into Job B (#524).

`app/cli/sl_desk_tick.py`'s commentary step (#516) originally wired only
``detect_percentile`` -- the other seven detectors were implemented and
unit-tested (#520) but never invoked because no ticket supplied their
caller-fetched peer populations. `app/services/summer_league/desk_fact_queries.py`
(#524) is that read layer; this file proves the end-to-end wiring:

* a seeded multi-player, multi-game fixture persists **more than one distinct
  FactKind** onto T2/T4 ``facts`` (not just the lone ``percentile`` #516
  shipped);
* the #518 subsumption rule -- a rank-1 ``cohort_rank`` subsumes its own
  ``percentile`` on the same (player, competition, metric, cohort) axis --
  actually fires end to end (the persisted percentile entry's ``prose`` is
  suppressed while ``cohort_rank``'s is populated).

No live network calls: the NBA Stats client is always given a fake
``curl_cffi``-compatible session (mirrors `test_sl_desk_tick.py`).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
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
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.cli.sl_desk_tick import run_desk_tick

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


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
        """Return the response registered for the requested LeagueID."""
        league_id = params.get("LeagueID", "")
        if league_id not in self.responses_by_league:
            return FakeResponse({}, status_code=404)
        return self.responses_by_league[league_id]

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


def _empty_schedule_payload() -> dict[str, Any]:
    return {"leagueSchedule": {"gameDates": []}}


async def _seed_competition(db: AsyncSession, *, year: int) -> SummerLeagueEdition:
    idx = _next_idx()
    comp = SummerLeagueEdition(
        year=year,
        league_id="15",
        venue_slug=f"las_vegas-{idx}",
        display_name=f"{year} Las Vegas",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
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
    game_date: date,
) -> SummerLeagueGame:
    idx = _next_idx()
    assert competition.id is not None
    assert home.id is not None
    assert away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"fact-wiring-game-{idx}",
        game_date=game_date,
        tip_datetime=datetime(game_date.year, game_date.month, game_date.day, 18, 0),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=SummerLeagueGameStatus.FINAL,
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

    db.add(
        SummerLeagueParticipation(
            competition_id=competition.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=player.id,
            roster_status=AffiliationStatus.ACTIVE,
        )
    )
    await db.flush()
    return source_player


async def _seed_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    source_player: SummerLeagueSourcePlayer,
    player: PlayerMaster,
    pts: int,
    ast: int,
    reb: int,
) -> None:
    """A single strong-ish box line -- values only need to rank sensibly."""
    assert competition.id is not None
    assert game.id is not None
    assert team.id is not None
    assert source_player.id is not None
    assert player.id is not None
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=player.display_name or "Test Player",
            pts=pts,
            fgm=max(1, pts // 3),
            fga=max(2, pts // 2),
            ftm=2,
            fta=3,
            oreb=1,
            dreb=max(0, reb - 1),
            reb=reb,
            ast=ast,
            stl=1,
            blk=0,
            tov=1,
            pf=2,
        )
    )
    await db.flush()


async def _seed_season(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    player: PlayerMaster,
    year: int,
    gmsc: float,
    minutes: float,
    gp: int,
) -> None:
    assert competition.id is not None
    assert player.id is not None
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=year,
            venue_slug=competition.venue_slug,
            is_current=True,
            gp=gp,
            minutes=minutes,
            gmsc=gmsc,
        )
    )
    await db.flush()


async def _seed_event_baseline(
    db: AsyncSession, *, baseline_version: str, cohort_key: str
) -> SummerLeagueCohortBaseline:
    baseline = SummerLeagueCohortBaseline(
        baseline_version=baseline_version,
        is_active=True,
        cohort_key=cohort_key,
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        metric="gmsc",
        grain=SummerLeagueDeskGrain.EVENT,
        venue_scope="all",
        season_range="2017-2025",
        min_minutes=0.0,
        n_members=20,
        breakpoints={
            "0": 5.0,
            "25": 15.0,
            "50": 25.0,
            "75": 35.0,
            "90": 45.0,
            "100": 60.0,
        },
        mean_value=25.0,
        median_value=25.0,
    )
    db.add(baseline)
    await db.flush()
    return baseline


async def _seed_debut_baseline(
    db: AsyncSession, *, baseline_version: str, cohort_key: str
) -> SummerLeagueCohortBaseline:
    baseline = SummerLeagueCohortBaseline(
        baseline_version=baseline_version,
        is_active=True,
        cohort_key=cohort_key,
        cohort_kind=SummerLeagueDeskCohortKind.DEBUT,
        metric="gmsc",
        grain=SummerLeagueDeskGrain.DEBUT,
        venue_scope="all",
        season_range="2017-2025",
        min_minutes=0.0,
        n_members=15,
        breakpoints={"0": 2.0, "50": 12.0, "100": 22.0},
        mean_value=12.0,
        median_value=12.0,
    )
    db.add(baseline)
    await db.flush()
    return baseline


async def _seed_fixture(db: AsyncSession) -> dict[str, Any]:
    """A multi-player, multi-game fixture that fires >=4 distinct FactKinds.

    ``Rookie`` (draft_round=1, draft_pick=1 -> cohort ``slot:1-4``) is
    debuting, blows away a lone 2025 historical peer sharing the exact same
    cohort_key (``cohort_rank`` rank-1), clears the ``debut:1-4`` bar
    (``debut_vs_bar``), and outscores ``Bench`` tonight (``leads_field``) --
    on top of the always-firing ``percentile``.
    """
    now = datetime(2026, 7, 10, 20, 0)  # 4:00pm ET (EDT, UTC-4)
    year = 2026
    game_date = date(2026, 7, 10)

    competition = await _seed_competition(db, year=year)
    home = await _seed_team(db, competition)
    away = await _seed_team(db, competition)
    game = await _seed_game(db, competition, home, away, game_date=game_date)

    rookie = await _seed_player(db, name="Rookie", draft_round=1, draft_pick=1)
    rookie_source = await _roster_player(db, competition, home, rookie)
    await _seed_game_log(
        db,
        competition=competition,
        game=game,
        team=home,
        source_player=rookie_source,
        player=rookie,
        pts=32,
        ast=6,
        reb=8,
    )
    await _seed_season(
        db,
        competition=competition,
        player=rookie,
        year=year,
        gmsc=75.0,
        minutes=150.0,
        gp=5,
    )

    bench = await _seed_player(db, name="Bench", draft_round=2, draft_pick=20)
    bench_source = await _roster_player(db, competition, away, bench)
    await _seed_game_log(
        db,
        competition=competition,
        game=game,
        team=away,
        source_player=bench_source,
        player=bench,
        pts=6,
        ast=1,
        reb=2,
    )

    # A 2025 historical peer sharing Rookie's EXACT cohort_key ("slot:1-4":
    # also draft_round=1, draft_pick=1) so `cohort_rank` has a non-empty,
    # beatable peer population -- the mockup's "ahead of 2025 Flagg" shape.
    historical_peer = await _seed_player(
        db, name="Peer2025", draft_round=1, draft_pick=1
    )
    await _seed_season(
        db,
        competition=competition,
        player=historical_peer,
        year=2025,
        gmsc=18.9,
        minutes=100.0,
        gp=4,
    )

    baseline_version = "sl-desk-fact-wiring-v1"
    event_baseline = await _seed_event_baseline(
        db, baseline_version=baseline_version, cohort_key="slot:1-4"
    )
    await _seed_debut_baseline(
        db, baseline_version=baseline_version, cohort_key="debut:1-4"
    )
    await db.commit()

    return {
        "now": now,
        "competition": competition,
        "game": game,
        "rookie": rookie,
        "bench": bench,
        "historical_peer": historical_peer,
        "baseline_version": baseline_version,
        "event_baseline": event_baseline,
    }


async def test_desk_tick_persists_more_than_one_fact_kind(
    db_session: AsyncSession,
) -> None:
    """A seeded multi-player, multi-game night fires >1 distinct FactKind onto T2/T4.

    #516 wired only ``detect_percentile``, so before #524 every persisted
    ``facts`` payload carried exactly one kind. This fixture fires
    ``percentile`` (always), ``cohort_rank`` (Rookie ranks #1 vs the 2025
    historical peer), ``debut_vs_bar`` (Rookie is debuting), and
    ``leads_field`` (Rookie outscores Bench tonight).
    """
    fixture = await _seed_fixture(db_session)
    rookie = fixture["rookie"]
    competition = fixture["competition"]
    assert rookie.id is not None
    assert competition.id is not None

    session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    client = NBAStatsClient(session=session)

    result = await run_desk_tick(db_session, now=fixture["now"], client=client)
    await db_session.commit()

    assert result.dormant is False
    assert rookie.id in result.graded_player_ids

    grade_row = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == rookie.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == competition.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version
                == fixture["baseline_version"],  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert grade_row.facts, "expected a non-empty facts payload"
    kinds = {entry["kind"] for entry in grade_row.facts}
    assert len(kinds) > 1, f"expected >1 distinct FactKind, got {kinds}"
    # Confirms the wiring reaches well beyond the lone `percentile` #516 shipped.
    assert "percentile" in kinds
    assert "cohort_rank" in kinds
    assert "debut_vs_bar" in kinds
    assert "leads_field" in kinds

    # T4 slate row for tonight's (only) game carries the same fired kinds,
    # grouped across both rostered players.
    slate_row = (
        await db_session.execute(
            select(SummerLeagueDeskSlate).where(
                SummerLeagueDeskSlate.game_id == fixture["game"].id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert slate_row.facts
    slate_kinds = {entry["kind"] for entry in slate_row.facts}
    assert len(slate_kinds) > 1


async def test_desk_tick_cohort_rank_subsumes_percentile_end_to_end(
    db_session: AsyncSession,
) -> None:
    """#518's rank-1 `cohort_rank` -> `percentile` subsumption fires end to end.

    Rookie's `cohort_rank` (rank=1, beating the sole 2025 historical peer) and
    `percentile` share the same (player, competition, metric="gmsc",
    cohort="slot:1-4") axis, so Stage 2 selection subsumes the percentile:
    its persisted entry's rendered `prose` must be suppressed (`None`) while
    `cohort_rank`'s prose is populated. Before #524 wired `cohort_rank` at
    all, this axis could never even exist -- `percentile` was the only kind
    ever fired, so the rule was dead code.
    """
    fixture = await _seed_fixture(db_session)
    rookie = fixture["rookie"]
    competition = fixture["competition"]
    assert rookie.id is not None
    assert competition.id is not None

    session = FakeSession({"15": FakeResponse(_empty_schedule_payload())})
    client = NBAStatsClient(session=session)

    await run_desk_tick(db_session, now=fixture["now"], client=client)
    await db_session.commit()

    grade_row = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == rookie.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == competition.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version
                == fixture["baseline_version"],  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert grade_row.facts

    by_kind: dict[Any, dict[str, Any]] = {
        entry["kind"]: entry for entry in grade_row.facts
    }
    assert "cohort_rank" in by_kind
    assert "percentile" in by_kind

    cohort_rank_entry = by_kind["cohort_rank"]
    percentile_entry = by_kind["percentile"]

    # Both facts are on the identical axis -- same cohort, same subject.
    assert cohort_rank_entry["cohort"] == percentile_entry["cohort"] == "slot:1-4"
    assert cohort_rank_entry["values"]["rank"] == 1

    # The subsumption rule: cohort_rank keeps its prose; percentile's is
    # suppressed (never selected for a prose surface), though it still
    # renders a chip regardless (chips bypass selection per #518).
    assert cohort_rank_entry["prose"] is not None
    assert "tick_note" in cohort_rank_entry["selected_for"]
    assert percentile_entry["prose"] is None
    assert percentile_entry["selected_for"] == []
    assert percentile_entry["chip"]  # chip still renders despite being subsumed

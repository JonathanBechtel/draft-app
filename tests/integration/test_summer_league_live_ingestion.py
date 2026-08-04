"""Integration tests for targeted live raw refresh selection (ticket #531).

Launch-readiness plan step 2 ("Targeted live box-score refresh"):
``app.services.sources.summer_league.live_ingestion`` selects active/recently-final
``summer_league_games`` rows within an active time window and force-refreshes
only those games' raw NBA Stats endpoints.

Games are seeded from a REAL captured schedule payload, not hand-authored
dicts -- repo convention (see
``tests/integration/test_summer_league_scoreboard_ingest.py``):

* ``scheduleleaguev2_15_2026_live_pretip.json`` -- captured live from
  stats.nba.com (LeagueID 15, Season 2026) on 2026-07-10 ~19:34 UTC: seven
  real Final games (2026-07-09 19:30 through 2026-07-10 03:00 UTC) plus real
  not-yet-tipped Scheduled games spanning 2026-07-10 through 2026-07-19.

No live network call is made anywhere in this file -- ``run_live_ingestion``
is exercised with a recording fake NBA Stats client.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.sources.summer_league.live_ingestion import (
    run_live_ingestion,
    select_active_window_games,
)
from app.services.sources.summer_league.raw_ingestion import GAME_ENDPOINTS
from app.services.sources.summer_league.raw_store import SummerLeagueRawStore
from app.services.sources.summer_league.scoreboard_ingest import (
    ScoreboardGame,
    parse_scoreboard_games,
    upsert_scoreboard_games,
)

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
_REAL_LIVE_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"

# The real fixture's Scheduled games tipping inside a [now-6h, now+6h] window
# anchored at 2026-07-10T19:30 (naive UTC) -- see module docstring for the
# fixture's real contents. 1522600014 (07-11 02:00) is the real next game
# past the window_after cutoff (07-11 01:30) -- a genuine "distant" exclusion.
_EXPECTED_WINDOW_GAME_IDS = {
    "1522600008",
    "1522600009",
    "1522600010",
    "1522600011",
    "1522600012",
    "1522600013",
}

_N = {"i": 0}


def _load_fixture(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


async def _competition(
    db: AsyncSession, *, year: int, league_id: str, venue: str
) -> SummerLeagueEdition:
    _N["i"] += 1
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=f"{venue}-{_N['i']}",
        display_name=f"{year} {venue}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    return comp


async def _seed_real_games(db: AsyncSession, competition: SummerLeagueEdition) -> None:
    """Seed summer_league_games from the real captured 2026 live-capture fixture."""
    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    games = parse_scoreboard_games(payload, target_dates=None)
    assert competition.id is not None
    await upsert_scoreboard_games(db, competition_id=competition.id, games=games)
    await db.flush()


class FakeNBAStatsClient:
    """Recording fake NBA Stats client -- run_live_ingestion never touches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, object]:
        """Record the call and return a minimal deterministic fake payload."""
        clean_params = dict(params)
        self.calls.append((endpoint, clean_params))
        if endpoint == "leaguegamelog":
            return {
                "resultSets": [
                    {"name": "LeagueGameLog", "headers": ["GAME_ID"], "rowSet": []}
                ]
            }
        return {
            "resultSets": [
                {
                    "name": endpoint,
                    "headers": ["GAME_ID"],
                    "rowSet": [[clean_params.get("GameID") or "unknown"]],
                }
            ]
        }


@pytest.mark.asyncio
async def test_select_active_window_games_selects_scheduled_within_window_only(
    db_session: AsyncSession,
) -> None:
    """Only Scheduled/In-Progress games with a tip_datetime inside the window are selected."""
    comp = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _seed_real_games(db_session, comp)
    await db_session.commit()

    now = datetime(2026, 7, 10, 19, 30)  # naive UTC
    selections = await select_active_window_games(db_session, now=now)

    selected_ids = {s.nba_stats_game_id for s in selections}
    assert selected_ids == _EXPECTED_WINDOW_GAME_IDS
    # A real game tipping 30 minutes past the window_after cutoff is excluded --
    # "distant" games are skipped even though their status is still Scheduled.
    assert "1522600014" not in selected_ids
    for selection in selections:
        assert selection.year == 2026
        assert selection.league_id == "15"


@pytest.mark.asyncio
async def test_select_active_window_games_selects_only_finals_missing_player_lines(
    db_session: AsyncSession,
) -> None:
    """Recent Finals get a closing pull only while player lines are still missing.

    All seven real Final games (2026-07-09 19:30 through 2026-07-10 03:00 UTC)
    fall inside this narrower window; no Scheduled game does (the next real
    game tips at 2026-07-10 20:00 UTC). One gets a normalized player line to
    prove healthy Finals are skipped; the other six still need recovery.
    """
    comp = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _seed_real_games(db_session, comp)
    assert comp.id is not None
    healthy_game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522600001"
            )
        )
    ).scalar_one()
    assert healthy_game.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="healthy-final-team",
        raw_team_name="Healthy Final Team",
        team_slug="healthy-final-team",
    )
    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id="healthy-final-player",
        raw_player_name="Healthy Final Player",
        normalized_name="healthy final player",
    )
    db_session.add_all([team, source_player])
    await db_session.flush()
    assert team.id is not None
    assert source_player.id is not None
    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=healthy_game.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=source_player.raw_player_name,
            pts=12,
        )
    )
    await db_session.commit()

    now = datetime(2026, 7, 10, 1, 0)
    selections = await select_active_window_games(db_session, now=now)

    assert len(selections) == 6
    assert {selection.nba_stats_game_id for selection in selections} == {
        "1522600002",
        "1522600003",
        "1522600004",
        "1522600005",
        "1522600006",
        "1522600007",
    }


@pytest.mark.asyncio
async def test_select_active_window_games_excludes_postponed_game_inside_window(
    db_session: AsyncSession,
) -> None:
    """A POSTPONED game whose tip falls inside the active window is still excluded.

    Core regression for fix #4: a prior fix made postponed games terminal in the
    event-agnostic daily state machine, but the DB status column still persisted
    them as SCHEDULED -- so `_LIVE_STATUSES` (which filters on this column) kept
    selecting a postponed game for a critical box-score refresh whose endpoints
    will never return data, which (combined with fix #2's fail-the-whole-tick
    guard) would abort every tick in its window. `map_game_status` now persists
    the real POSTPONED status, so `_LIVE_STATUSES` excludes it with no special
    casing -- proven here against a game tipping squarely inside the window.
    """
    comp = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    assert comp.id is not None
    await upsert_scoreboard_games(
        db_session,
        competition_id=comp.id,
        games=[
            ScoreboardGame(
                nba_stats_game_id="postponed-1",
                game_date=date(2026, 7, 10),
                tip_datetime=datetime(2026, 7, 10, 20, 0),
                status=SummerLeagueGameStatus.POSTPONED,
                status_text="PPD",
            )
        ],
    )
    await db_session.commit()

    now = datetime(2026, 7, 10, 19, 30)
    selections = await select_active_window_games(db_session, now=now)

    assert "postponed-1" not in {s.nba_stats_game_id for s in selections}


@pytest.mark.asyncio
async def test_select_active_window_games_scopes_to_the_requesting_competition_year_league(
    db_session: AsyncSession,
) -> None:
    """Two competitions' games are each tagged with their own year/LeagueID."""
    vegas = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _seed_real_games(db_session, vegas)
    other = await _competition(
        db_session, year=2025, league_id="13", venue="california"
    )
    assert other.id is not None
    await upsert_scoreboard_games(
        db_session,
        competition_id=other.id,
        games=[
            ScoreboardGame(
                nba_stats_game_id="other-1",
                game_date=date(2026, 7, 10),
                tip_datetime=datetime(2026, 7, 10, 20, 0),
                status=SummerLeagueGameStatus.SCHEDULED,
            )
        ],
    )
    await db_session.commit()

    now = datetime(2026, 7, 10, 19, 30)
    selections = await select_active_window_games(db_session, now=now)

    by_id = {s.nba_stats_game_id: s for s in selections}
    assert by_id["1522600008"].year == 2026
    assert by_id["1522600008"].league_id == "15"
    assert by_id["other-1"].year == 2025
    assert by_id["other-1"].league_id == "13"


@pytest.mark.asyncio
async def test_run_live_ingestion_replaces_only_selected_games_with_no_live_network(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """End to end: selected games get force-refreshed; others are left untouched."""
    comp = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _seed_real_games(db_session, comp)
    await db_session.commit()

    store = SummerLeagueRawStore(tmp_path)
    stale_path = store.game_file(
        year=2026,
        league_id="15",
        game_id="1522600008",
        endpoint="boxscoretraditionalv2",
    )
    store.write_json(stale_path, {"stale": True})
    untouched_path = store.game_file(
        year=2026,
        league_id="15",
        game_id="1522600001",
        endpoint="boxscoretraditionalv2",
    )
    # 1522600001 is a real Final game from the fixture -- outside the active
    # window and must never be requested or written by a live-ingestion run.
    assert not untouched_path.exists()

    client = FakeNBAStatsClient()
    report = await run_live_ingestion(
        db_session,
        client=client,
        store=store,
        clock=lambda: datetime(2026, 7, 10, 19, 30),
        sleep=lambda _: None,
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    called_ids = {params["GameID"] for _, params in game_calls}
    assert called_ids == _EXPECTED_WINDOW_GAME_IDS
    assert json.loads(stale_path.read_text()) != {"stale": True}
    assert not untouched_path.exists()
    assert report.selected == 6
    assert report.groups == 1
    assert report.errors == 0
    assert report.written > 0


@pytest.mark.asyncio
async def test_run_live_ingestion_runs_boundary_only_when_refreshing_games(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The transaction boundary runs once before a non-empty provider refresh."""
    comp = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _seed_real_games(db_session, comp)
    await db_session.commit()

    boundary_calls = 0

    async def record_boundary() -> None:
        nonlocal boundary_calls
        boundary_calls += 1

    await run_live_ingestion(
        db_session,
        client=FakeNBAStatsClient(),
        store=SummerLeagueRawStore(tmp_path),
        clock=lambda: datetime(2026, 7, 10, 19, 30),
        sleep=lambda _: None,
        before_refresh=record_boundary,
    )

    assert boundary_calls == 1


@pytest.mark.asyncio
async def test_run_live_ingestion_empty_selection_makes_no_network_calls(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """When nothing is in the active window, run_live_ingestion issues zero fetch calls."""
    comp = await _competition(db_session, year=2026, league_id="15", venue="las_vegas")
    await _seed_real_games(db_session, comp)
    await db_session.commit()

    client = FakeNBAStatsClient()
    boundary_calls = 0

    async def record_boundary() -> None:
        nonlocal boundary_calls
        boundary_calls += 1

    report = await run_live_ingestion(
        db_session,
        client=client,
        store=SummerLeagueRawStore(tmp_path),
        clock=lambda: datetime(2020, 1, 1, 0, 0),
        sleep=lambda _: None,
        before_refresh=record_boundary,
    )

    assert client.calls == []
    assert boundary_calls == 0
    assert report.selected == 0
    assert report.groups == 0

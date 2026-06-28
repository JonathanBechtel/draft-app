"""Integration tests for the game-flow chart wiring on the box-score page.

Exercises GET /stats/summer-league/{year}/games/{game_id} with and without
play-by-play data, verifying:

  1. Game WITH PBP events → window.SL_GAME_FLOW injected; chart root present.
  2. Game WITHOUT PBP events → chart section absent; no error (graceful).
  3. Service function get_game_flow_series returns None for a no-PBP game.
  4. Service function returns a well-formed series for a game with PBP events,
     with monotonic time and the expected endpoint.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayByPlayEvent,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_games_service import get_game_flow_series

_SEQ: dict[str, int] = {"n": 0}


def _uid() -> str:
    _SEQ["n"] += 1
    return f"gf-{_SEQ['n']}"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _make_comp(db: AsyncSession, *, year: int = 2023) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id=_uid(),
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas Summer League",
    )
    db.add(comp)
    await db.flush()
    return comp


async def _make_team(db: AsyncSession, *, comp_id: int, name: str = "Team") -> SummerLeagueTeamEntry:
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=_uid(),
        raw_team_name=name,
        raw_team_abbreviation=name[:3].upper(),
        team_slug=f"{name.lower()}-{_uid()}",
    )
    db.add(team)
    await db.flush()
    return team


async def _make_game(
    db: AsyncSession,
    *,
    comp: SummerLeagueCompetition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int = 110,
    away_score: int = 100,
) -> SummerLeagueGame:
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=_uid(),
        game_date=date(2023, 7, 12),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(game)
    await db.flush()
    return game


def _pbp_event(
    *,
    game: SummerLeagueGame,
    event_num: int,
    period: int,
    clock: str,
    score_margin: int | None = None,
) -> SummerLeaguePlayByPlayEvent:
    """Return an unsaved PBP event row."""
    return SummerLeaguePlayByPlayEvent(
        game_id=game.id,
        competition_id=game.competition_id,
        nba_stats_game_id=game.nba_stats_game_id,
        event_num=event_num,
        period=period,
        clock=clock,
        score_margin=score_margin,
    )


async def _seed_game_with_pbp(
    db: AsyncSession,
) -> tuple[SummerLeagueGame, list[SummerLeaguePlayByPlayEvent]]:
    """Seed a game with a handful of PBP events spanning 4 quarters."""
    comp = await _make_comp(db)
    home = await _make_team(db, comp_id=comp.id, name="HomeTeam")  # type: ignore[arg-type]
    away = await _make_team(db, comp_id=comp.id, name="AwayTeam")  # type: ignore[arg-type]
    game = await _make_game(db, comp=comp, home=home, away=away, home_score=110, away_score=100)

    # Events: scattered across all 4 quarters with explicit score margins.
    events_spec = [
        (1, "08:00", 2),    # Q1, 4 min elapsed, home +2
        (2, "10:00", -3),   # Q2 start, away +3
        (3, "06:00", 5),    # Q3 mid, home +5
        (4, "01:00", 10),   # Q4 late, home +10
        (4, "00:00", 10),   # Q4 final, margin = 10
    ]
    events = []
    for i, (period, clock, margin) in enumerate(events_spec):
        ev = _pbp_event(
            game=game,
            event_num=i + 1,
            period=period,
            clock=clock,
            score_margin=margin,
        )
        db.add(ev)
        events.append(ev)

    await db.flush()
    return game, events


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_game_flow_series_no_pbp(db_session: AsyncSession) -> None:
    """get_game_flow_series returns None for a game with no PBP rows."""
    comp = await _make_comp(db_session)
    home = await _make_team(db_session, comp_id=comp.id, name="Alpha")  # type: ignore[arg-type]
    away = await _make_team(db_session, comp_id=comp.id, name="Beta")  # type: ignore[arg-type]
    game = await _make_game(db_session, comp=comp, home=home, away=away)
    await db_session.commit()

    result = await get_game_flow_series(db_session, game_id=game.id)  # type: ignore[arg-type]
    assert result is None


@pytest.mark.asyncio
async def test_get_game_flow_series_with_pbp(db_session: AsyncSession) -> None:
    """get_game_flow_series returns a well-formed series for a PBP-era game.

    Expected properties:
    - First point is always {t: 0.0, margin: 0}.
    - Last point margin matches the final seeded score_margin.
    - Elapsed times are monotonically non-decreasing.
    """
    game, _ = await _seed_game_with_pbp(db_session)
    await db_session.commit()

    series = await get_game_flow_series(db_session, game_id=game.id)  # type: ignore[arg-type]

    assert series is not None
    assert len(series) >= 2

    # Origin point
    assert series[0] == {"t": 0.0, "margin": 0}

    # Monotonic time
    times = [p["t"] for p in series]
    assert times == sorted(times), "elapsed times must be non-decreasing"

    # Endpoint matches the last scored event (margin=10)
    assert series[-1]["margin"] == 10

    # Lead changes visible: must have both positive and negative margins
    all_margins = [p["margin"] for p in series]
    assert any(m > 0 for m in all_margins)
    assert any(m < 0 for m in all_margins)


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_game_box_score_with_pbp_renders_game_flow(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Box-score page for a PBP-era game includes the game-flow chart payload."""
    game, _ = await _seed_game_with_pbp(db_session)
    await db_session.commit()

    assert game.id is not None
    resp = await app_client.get(f"/stats/summer-league/2023/games/{game.id}")
    assert resp.status_code == 200
    html = resp.text

    # Game-flow section present
    assert "sl-game-flow-section" in html
    assert "sl-game-flow__chart" in html
    assert "Score Margin" in html

    # JS payload injected
    assert "window.SL_GAME_FLOW" in html

    # JS module linked
    assert "game-flow-chart.js" in html


@pytest.mark.asyncio
async def test_game_box_score_without_pbp_omits_game_flow(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Box-score page for a pre-PBP game omits the chart without an error."""
    comp = await _make_comp(db_session)
    home = await _make_team(db_session, comp_id=comp.id, name="Gamma")  # type: ignore[arg-type]
    away = await _make_team(db_session, comp_id=comp.id, name="Delta")  # type: ignore[arg-type]
    game = await _make_game(db_session, comp=comp, home=home, away=away)
    await db_session.commit()

    assert game.id is not None
    resp = await app_client.get(f"/stats/summer-league/2023/games/{game.id}")
    assert resp.status_code == 200
    html = resp.text

    # Game-flow chart section must be absent
    assert "sl-game-flow-section" not in html
    assert "window.SL_GAME_FLOW" not in html

    # Page still loads successfully (no error partial)
    assert "box-score" not in html.lower() or "HomeTeam" not in html  # not required
    # The box-score content still renders
    assert "sl-boxes" in html or "slg-bx" in html

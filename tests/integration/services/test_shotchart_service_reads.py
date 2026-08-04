"""Integration tests for summer_league_shotchart_service (DB-backed).

Seeds ``SummerLeagueShotEvent`` rows directly via the DB session (bypassing the
parser) and exercises the three public service functions against real Postgres:

1. ``get_player_shot_zones`` — per-zone FGA/FGM/FG%/freq% and pool baseline.
2. ``get_player_shot_dots`` — raw (loc_x, loc_y, made) list.
3. ``get_game_shot_zones`` — game-scoped zone aggregation with optional filters.
4. Career rollup: two competitions summed additively; pool_fg_pct=None.

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueShotEvent,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_shotchart_service import (
    MIN_FGA_FOR_CHART,
    get_game_shot_zones,
    get_player_shot_dots,
    get_player_shot_zones,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


async def _make_competition(
    db: AsyncSession, *, year: int = 2024, league_id: str = "15"
) -> SummerLeagueEdition:
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug="las_vegas",
        display_name=f"Las Vegas {year}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 14),
        data_quality=SummerLeagueDataQuality.RAW_ONLY,
    )
    db.add(comp)
    await db.flush()
    return comp


async def _make_team_entry(
    db: AsyncSession, *, competition_id: int, nba_stats_team_id: str = "1610612753"
) -> SummerLeagueTeamEntry:
    entry = SummerLeagueTeamEntry(
        competition_id=competition_id,
        nba_stats_team_id=nba_stats_team_id,
        raw_team_name="Test Team",
        team_slug=f"team-{nba_stats_team_id}",
    )
    db.add(entry)
    await db.flush()
    return entry


async def _make_game(
    db: AsyncSession,
    *,
    competition_id: int,
    nba_stats_game_id: str = "1522400001",
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
    return game


async def _make_player(db: AsyncSession, *, display_name: str, slug: str) -> PlayerMaster:
    player = PlayerMaster(display_name=display_name, slug=slug)
    db.add(player)
    await db.flush()
    return player


def _shot(
    *,
    game_id: int,
    competition_id: int,
    team_entry_id: int,
    source_player_id: int,
    player_id: int | None,
    nba_stats_person_id: str,
    nba_stats_game_id: str = "1522400001",
    nba_stats_game_event_id: int,
    shot_zone_basic: str = "Restricted Area",
    loc_x: int = 0,
    loc_y: int = 50,
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
        nba_stats_game_event_id=nba_stats_game_event_id,
        shot_zone_basic=shot_zone_basic,
        shot_zone_area="Center(C)",
        shot_zone_range="Less Than 8 ft.",
        shot_type="2PT Field Goal",
        loc_x=loc_x,
        loc_y=loc_y,
        shot_distance=2,
        made=made,
        period=1,
        minutes_remaining=9,
        seconds_remaining=0,
    )


# We need a SummerLeagueSourcePlayer for FK; import here to avoid confusion at top.
async def _make_source_player(
    db: AsyncSession,
    *,
    nba_stats_person_id: str,
    competition_id: int,
    canonical_player_id: int | None = None,
) -> "SummerLeagueSourcePlayer":  # type: ignore[name-defined]
    from app.schemas.summer_league import (
        SummerLeagueResolutionStatus,
        SummerLeagueSourcePlayer,
    )

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
    return sp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_player_shot_zones_single_competition(db_session: AsyncSession) -> None:
    """Zone FGA/FGM/FG% aggregated correctly from seeded shot rows.

    Seeds:
    - Player A: 8 RA shots (5 made), 6 Mid-Range (2 made), 10 AB3 (4 made)
    - Total: 24 FGA → not suppressed
    """
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Player A", slug="player-a")
    assert player.id is not None
    sp = await _make_source_player(
        db_session,
        nba_stats_person_id="1111",
        competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    # Seed shot rows: 8 RA (5 made), 6 Mid-Range (2 made), 10 AB3 (4 made)
    shot_event_id = 1
    for i in range(8):
        db_session.add(
            _shot(
                game_id=game.id,
                competition_id=comp.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id="1111",
                nba_stats_game_event_id=shot_event_id,
                shot_zone_basic="Restricted Area",
                made=(i < 5),
            )
        )
        shot_event_id += 1
    for i in range(6):
        db_session.add(
            _shot(
                game_id=game.id,
                competition_id=comp.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id="1111",
                nba_stats_game_event_id=shot_event_id,
                shot_zone_basic="Mid-Range",
                made=(i < 2),
            )
        )
        shot_event_id += 1
    for i in range(10):
        db_session.add(
            _shot(
                game_id=game.id,
                competition_id=comp.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id="1111",
                nba_stats_game_event_id=shot_event_id,
                shot_zone_basic="Above the Break 3",
                made=(i < 4),
            )
        )
        shot_event_id += 1
    await db_session.flush()

    result = await get_player_shot_zones(db_session, player_id=player.id, competition_id=comp.id)

    assert result.total_fga == 24
    assert result.suppressed is False

    by_zone = {z.shot_zone_basic: z for z in result.zones}
    assert "Restricted Area" in by_zone
    ra = by_zone["Restricted Area"]
    assert ra.fga == 8
    assert ra.fgm == 5
    assert ra.fg_pct == pytest.approx(5 / 8)
    assert ra.freq_pct == pytest.approx(8 / 24)

    mr = by_zone["Mid-Range"]
    assert mr.fga == 6
    assert mr.fgm == 2
    assert mr.fg_pct == pytest.approx(2 / 6)

    ab3 = by_zone["Above the Break 3"]
    assert ab3.fga == 10
    assert ab3.fgm == 4


@pytest.mark.asyncio
async def test_get_player_shot_zones_pool_baseline(db_session: AsyncSession) -> None:
    """pool_fg_pct reflects the competition pool, not just the target player.

    Seeds:
    - Player A: 2 RA shots, 1 made (50%)
    - Player B: 4 RA shots, 3 made (75%)
    - Pool avg = 4/6 ≈ 66.7%
    - Player A should see pool_fg_pct ≈ 0.667.
    """
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None

    player_a = await _make_player(db_session, display_name="Player A", slug="player-a")
    player_b = await _make_player(db_session, display_name="Player B", slug="player-b")
    assert player_a.id is not None
    assert player_b.id is not None
    sp_a = await _make_source_player(
        db_session, nba_stats_person_id="1001", competition_id=comp.id,
        canonical_player_id=player_a.id,
    )
    sp_b = await _make_source_player(
        db_session, nba_stats_person_id="1002", competition_id=comp.id,
        canonical_player_id=player_b.id,
    )
    assert sp_a.id is not None
    assert sp_b.id is not None

    # Ensure enough FGA for suppression check (we'll add extras in AB3 for player A)
    event_id = 1
    for made in [True, False]:  # 2 RA shots for A, 1 made
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp_a.id, player_id=player_a.id,
                nba_stats_person_id="1001", nba_stats_game_event_id=event_id,
                shot_zone_basic="Restricted Area", made=made,
            )
        )
        event_id += 1
    for made in [True, True, True, False]:  # 4 RA shots for B, 3 made
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp_b.id, player_id=player_b.id,
                nba_stats_person_id="1002", nba_stats_game_event_id=event_id,
                shot_zone_basic="Restricted Area", made=made,
            )
        )
        event_id += 1
    await db_session.flush()

    result = await get_player_shot_zones(
        db_session, player_id=player_a.id, competition_id=comp.id
    )

    # Player A has 2 RA FGA total; the zone must exist
    by_zone = {z.shot_zone_basic: z for z in result.zones}
    assert "Restricted Area" in by_zone
    ra = by_zone["Restricted Area"]
    # Player A's FG% = 1/2 = 50%
    assert ra.fg_pct == pytest.approx(0.5)
    # Pool FG% = (1+3)/(2+4) = 4/6 ≈ 66.7%
    assert ra.pool_fg_pct == pytest.approx(4 / 6)


@pytest.mark.asyncio
async def test_get_player_shot_zones_suppressed_below_floor(db_session: AsyncSession) -> None:
    """Fewer than MIN_FGA_FOR_CHART shots → suppressed=True."""
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Low Vol", slug="low-vol")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="2001", competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    # Seed only 5 shots (well below 20)
    for i in range(5):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="2001", nba_stats_game_event_id=i + 1,
                shot_zone_basic="Restricted Area", made=(i % 2 == 0),
            )
        )
    await db_session.flush()

    result = await get_player_shot_zones(db_session, player_id=player.id, competition_id=comp.id)

    assert result.suppressed is True
    assert result.total_fga == 5
    assert result.total_fga < MIN_FGA_FOR_CHART
    assert len(result.zones) == 1  # zone data still available for the table


@pytest.mark.asyncio
async def test_get_player_shot_zones_career_rollup(db_session: AsyncSession) -> None:
    """Career rollup sums shots across two competitions; pool_fg_pct=None.

    Seeds two competitions with the same player, each with 15 RA shots.
    Career total: 30 RA FGA → not suppressed.
    """
    comp_a = await _make_competition(db_session, year=2023, league_id="15")
    comp_b = await _make_competition(db_session, year=2024, league_id="15")
    assert comp_a.id is not None
    assert comp_b.id is not None

    team_a = await _make_team_entry(db_session, competition_id=comp_a.id, nba_stats_team_id="1111")
    team_b = await _make_team_entry(db_session, competition_id=comp_b.id, nba_stats_team_id="2222")
    assert team_a.id is not None
    assert team_b.id is not None
    game_a = await _make_game(db_session, competition_id=comp_a.id, nba_stats_game_id="1522300001")
    game_b = await _make_game(db_session, competition_id=comp_b.id, nba_stats_game_id="1522400001")
    assert game_a.id is not None
    assert game_b.id is not None

    player = await _make_player(db_session, display_name="Career Player", slug="career-player")
    assert player.id is not None
    # nba_stats_person_id is globally unique — one source-player record spans both comps.
    sp = await _make_source_player(
        db_session, nba_stats_person_id="3001", competition_id=comp_a.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    # 15 shots in comp_a
    for i in range(15):
        db_session.add(
            _shot(
                game_id=game_a.id, competition_id=comp_a.id, team_entry_id=team_a.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="3001", nba_stats_game_id="1522300001",
                nba_stats_game_event_id=i + 1,
                shot_zone_basic="Restricted Area", made=(i < 10),
            )
        )
    # 15 shots in comp_b
    for i in range(15):
        db_session.add(
            _shot(
                game_id=game_b.id, competition_id=comp_b.id, team_entry_id=team_b.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="3001", nba_stats_game_id="1522400001",
                nba_stats_game_event_id=i + 1,
                shot_zone_basic="Restricted Area", made=(i < 8),
            )
        )
    await db_session.flush()

    result = await get_player_shot_zones(db_session, player_id=player.id)  # no competition_id

    assert result.competition_id is None
    assert result.total_fga == 30
    assert result.suppressed is False
    assert len(result.zones) == 1
    ra = result.zones[0]
    assert ra.shot_zone_basic == "Restricted Area"
    assert ra.fga == 30
    assert ra.fgm == 18  # 10 + 8
    assert ra.fg_pct == pytest.approx(18 / 30)
    assert ra.pool_fg_pct is None  # career: no single pool baseline


@pytest.mark.asyncio
async def test_get_player_shot_dots_returns_coordinates(db_session: AsyncSession) -> None:
    """get_player_shot_dots returns correct (loc_x, loc_y, made) for seeded shots."""
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Dot Player", slug="dot-player")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="4001", competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    shots_data = [
        (-80, 100, True),
        (30, 200, False),
        (120, 50, True),
    ]
    for i, (x, y, made) in enumerate(shots_data):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="4001", nba_stats_game_event_id=i + 1,
                loc_x=x, loc_y=y, made=made,
            )
        )
    await db_session.flush()

    result = await get_player_shot_dots(db_session, player_id=player.id, competition_id=comp.id)

    assert result.player_id == player.id
    assert result.competition_id == comp.id
    assert len(result.dots) == 3
    coords = {(d.loc_x, d.loc_y, d.made) for d in result.dots}
    for expected in shots_data:
        assert expected in coords


@pytest.mark.asyncio
async def test_get_player_shot_dots_excludes_null_coordinates(db_session: AsyncSession) -> None:
    """Shots with null loc_x or loc_y are excluded from the dot list."""
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Sparse Dots", slug="sparse-dots")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="5001", competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    # Shot with null coordinates (loc_x=None)
    evt = SummerLeagueShotEvent(
        game_id=game.id,
        competition_id=comp.id,
        team_entry_id=team.id,
        source_player_id=sp.id,
        player_id=player.id,
        nba_stats_person_id="5001",
        nba_stats_game_id="1522400001",
        nba_stats_game_event_id=1,
        shot_zone_basic="Restricted Area",
        made=True,
        # loc_x and loc_y intentionally omitted → None
    )
    db_session.add(evt)
    # Shot with coordinates
    db_session.add(
        _shot(
            game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
            source_player_id=sp.id, player_id=player.id,
            nba_stats_person_id="5001", nba_stats_game_event_id=2,
            loc_x=10, loc_y=50, made=True,
        )
    )
    await db_session.flush()

    result = await get_player_shot_dots(db_session, player_id=player.id, competition_id=comp.id)

    # Only the shot with coordinates should be present
    assert len(result.dots) == 1
    assert result.dots[0].loc_x == 10


@pytest.mark.asyncio
async def test_get_player_shot_dots_career_spans_competitions(
    db_session: AsyncSession,
) -> None:
    """competition_id=None returns the player's shots across all competitions.

    Regression for the player-page career chart, which previously omitted dots
    (so the heat map never rendered). Career must aggregate dots across pools.
    """
    comp1 = await _make_competition(db_session, year=2023, league_id="15")
    comp2 = await _make_competition(db_session, year=2024, league_id="15")
    assert comp1.id is not None and comp2.id is not None
    player = await _make_player(db_session, display_name="Career Dots", slug="career-dots")
    assert player.id is not None

    for i, comp in enumerate((comp1, comp2)):
        cid = comp.id
        assert cid is not None
        team = await _make_team_entry(db_session, competition_id=cid)
        game = await _make_game(
            db_session, competition_id=cid,
            nba_stats_game_id=f"152{comp.year}0001",
        )
        sp = await _make_source_player(
            db_session, nba_stats_person_id=f"700{i}", competition_id=cid,
            canonical_player_id=player.id,
        )
        assert team.id is not None and game.id is not None and sp.id is not None
        db_session.add(
            _shot(
                game_id=game.id, competition_id=cid, team_entry_id=team.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id=f"700{i}", nba_stats_game_event_id=1,
                nba_stats_game_id=f"152{comp.year}0001",
                loc_x=10 * (i + 1), loc_y=40, made=True,
            )
        )
    await db_session.flush()

    career = await get_player_shot_dots(db_session, player_id=player.id)
    scoped = await get_player_shot_dots(
        db_session, player_id=player.id, competition_id=comp1.id
    )

    assert len(career.dots) == 2  # both competitions
    assert career.competition_id is None
    assert len(scoped.dots) == 1  # only comp1


@pytest.mark.asyncio
async def test_get_game_shot_zones_all_shots(db_session: AsyncSession) -> None:
    """Game-scoped zones sum all shots in the game."""
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Gamer", slug="gamer")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="6001", competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    for i in range(12):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="6001", nba_stats_game_event_id=i + 1,
                shot_zone_basic="Restricted Area" if i < 7 else "Above the Break 3",
                made=(i % 2 == 0),
            )
        )
    await db_session.flush()

    result = await get_game_shot_zones(db_session, game_id=game.id)

    assert result.game_id == game.id
    assert result.team_entry_id is None
    assert result.player_id is None
    assert result.total_fga == 12
    by_zone = {z.shot_zone_basic: z for z in result.zones}
    assert by_zone["Restricted Area"].fga == 7
    assert by_zone["Above the Break 3"].fga == 5


@pytest.mark.asyncio
async def test_get_game_shot_zones_filtered_by_team(db_session: AsyncSession) -> None:
    """Game zones filtered to a single team entry count only that team's shots."""
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team_a = await _make_team_entry(db_session, competition_id=comp.id, nba_stats_team_id="1111")
    team_b = await _make_team_entry(db_session, competition_id=comp.id, nba_stats_team_id="2222")
    assert team_a.id is not None
    assert team_b.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Team Player", slug="team-player")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="7001", competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    # 5 shots from team_a, 3 shots from team_b
    for i in range(5):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team_a.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="7001", nba_stats_game_event_id=i + 1,
                shot_zone_basic="Restricted Area", made=True,
            )
        )
    for i in range(3):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team_b.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="7001", nba_stats_game_event_id=i + 10,
                shot_zone_basic="Mid-Range", made=False,
            )
        )
    await db_session.flush()

    result = await get_game_shot_zones(db_session, game_id=game.id, team_entry_id=team_a.id)

    assert result.team_entry_id == team_a.id
    assert result.total_fga == 5
    by_zone = {z.shot_zone_basic: z for z in result.zones}
    assert "Restricted Area" in by_zone
    assert "Mid-Range" not in by_zone


@pytest.mark.asyncio
async def test_backcourt_shots_excluded_from_zone_agg(db_session: AsyncSession) -> None:
    """Backcourt shots are silently excluded from all zone aggregations."""
    comp = await _make_competition(db_session)
    assert comp.id is not None
    team = await _make_team_entry(db_session, competition_id=comp.id)
    assert team.id is not None
    game = await _make_game(db_session, competition_id=comp.id)
    assert game.id is not None
    player = await _make_player(db_session, display_name="Court Player", slug="court-player")
    assert player.id is not None
    sp = await _make_source_player(
        db_session, nba_stats_person_id="8001", competition_id=comp.id,
        canonical_player_id=player.id,
    )
    assert sp.id is not None

    # Seed 3 Restricted Area shots + 2 Backcourt shots
    for i in range(3):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="8001", nba_stats_game_event_id=i + 1,
                shot_zone_basic="Restricted Area", made=True,
            )
        )
    for i in range(2):
        db_session.add(
            _shot(
                game_id=game.id, competition_id=comp.id, team_entry_id=team.id,
                source_player_id=sp.id, player_id=player.id,
                nba_stats_person_id="8001", nba_stats_game_event_id=i + 10,
                shot_zone_basic="Backcourt", made=False,
            )
        )
    await db_session.flush()

    result = await get_player_shot_zones(db_session, player_id=player.id, competition_id=comp.id)

    # Only 3 FGA counted (Backcourt excluded)
    assert result.total_fga == 3
    zone_names = {z.shot_zone_basic for z in result.zones}
    assert "Backcourt" not in zone_names
    assert "Restricted Area" in zone_names

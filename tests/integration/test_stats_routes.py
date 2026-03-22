"""Integration tests for stats leaderboard routes and service."""

import pytest
import pytest_asyncio

from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.players_master import PlayerMaster
from app.schemas.player_status import PlayerStatus
from app.schemas.positions import Position
from app.schemas.seasons import Season
from app.services.combine_stats_service import get_leaderboard


# === Fixtures ===


async def _create_position(
    db_session, code: str, parents: list[str] | None = None
) -> Position:
    position = Position(code=code, description=code, parents=parents or [])
    db_session.add(position)
    await db_session.flush()
    return position


async def _create_season(
    db_session, code: str, start_year: int, end_year: int
) -> Season:
    season = Season(code=code, start_year=start_year, end_year=end_year)
    db_session.add(season)
    await db_session.flush()
    return season


async def _create_player(
    db_session,
    name: str,
    slug: str,
    *,
    school: str | None = None,
    draft_year: int | None = None,
    draft_round: int | None = None,
    draft_pick: int | None = None,
) -> PlayerMaster:
    player = PlayerMaster(
        display_name=name,
        slug=slug,
        school=school,
        draft_year=draft_year,
        draft_round=draft_round,
        draft_pick=draft_pick,
    )
    db_session.add(player)
    await db_session.flush()
    return player


async def _create_status(
    db_session,
    player: PlayerMaster,
    position: Position,
    *,
    is_active: bool = True,
) -> PlayerStatus:
    status = PlayerStatus(
        player_id=player.id,
        position_id=position.id,
        is_active_nba=is_active,
    )
    db_session.add(status)
    await db_session.flush()
    return status


async def _create_anthro(
    db_session,
    player: PlayerMaster,
    season: Season,
    *,
    wingspan: float | None = None,
    height_w_shoes: float | None = None,
    weight: float | None = None,
    position: Position | None = None,
) -> CombineAnthro:
    anthro = CombineAnthro(
        player_id=player.id,
        season_id=season.id,
        wingspan_in=wingspan,
        height_w_shoes_in=height_w_shoes,
        weight_lb=weight,
        position_id=position.id if position else None,
    )
    db_session.add(anthro)
    await db_session.flush()
    return anthro


async def _create_agility(
    db_session,
    player: PlayerMaster,
    season: Season,
    *,
    lane_agility: float | None = None,
    sprint: float | None = None,
    max_vertical: float | None = None,
) -> CombineAgility:
    agility = CombineAgility(
        player_id=player.id,
        season_id=season.id,
        lane_agility_time_s=lane_agility,
        three_quarter_sprint_s=sprint,
        max_vertical_in=max_vertical,
    )
    db_session.add(agility)
    await db_session.flush()
    return agility


@pytest_asyncio.fixture
async def seed_data(db_session):
    """Seed 5 players with combine anthro data across 2 seasons."""
    pos_c = await _create_position(db_session, "C", ["big"])
    pos_pg = await _create_position(db_session, "PG", ["guard"])

    s2024 = await _create_season(db_session, "2023-24", 2023, 2024)
    s2025 = await _create_season(db_session, "2024-25", 2024, 2025)

    # Players with varying wingspan
    p1 = await _create_player(
        db_session, "Tall Center", "tall-center",
        school="Big U", draft_year=2024, draft_round=1, draft_pick=1,
    )
    p2 = await _create_player(
        db_session, "Long Wing", "long-wing",
        school="Wing College", draft_year=2024, draft_round=1, draft_pick=10,
    )
    p3 = await _create_player(
        db_session, "Quick Guard", "quick-guard",
        school="Guard Academy", draft_year=2025, draft_round=2, draft_pick=35,
    )
    p4 = await _create_player(
        db_session, "Short Guard", "short-guard",
        school="Small School", draft_year=2025,
    )
    p5 = await _create_player(
        db_session, "No Draft", "no-draft",
        school="Overseas",
    )

    await _create_status(db_session, p1, pos_c, is_active=True)
    await _create_status(db_session, p2, pos_c, is_active=True)
    await _create_status(db_session, p3, pos_pg, is_active=True)
    await _create_status(db_session, p4, pos_pg, is_active=False)
    await _create_status(db_session, p5, pos_c, is_active=False)

    await _create_anthro(db_session, p1, s2024, wingspan=94.0, weight=250.0, position=pos_c)
    await _create_anthro(db_session, p2, s2024, wingspan=88.0, weight=220.0, position=pos_c)
    await _create_anthro(db_session, p3, s2025, wingspan=78.0, weight=185.0, position=pos_pg)
    await _create_anthro(db_session, p4, s2025, wingspan=72.0, weight=175.0, position=pos_pg)
    await _create_anthro(db_session, p5, s2024, wingspan=90.0, weight=240.0, position=pos_c)

    # Agility data for one player (to test asc sort)
    await _create_agility(db_session, p3, s2025, lane_agility=9.5, sprint=3.04)
    await _create_agility(db_session, p4, s2025, lane_agility=10.2, sprint=3.20)

    await db_session.commit()
    return {
        "players": [p1, p2, p3, p4, p5],
        "seasons": [s2024, s2025],
        "positions": [pos_c, pos_pg],
    }


# === Route Tests ===


@pytest.mark.asyncio
async def test_metric_page_returns_200(app_client, seed_data) -> None:
    """GET /stats/wingspan_in returns 200."""
    resp = await app_client.get("/stats/wingspan_in")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_metric_page_invalid_key_returns_404(app_client, seed_data) -> None:
    """GET /stats/fake_metric returns 404."""
    resp = await app_client.get("/stats/fake_metric")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metric_page_contains_player_names(app_client, seed_data) -> None:
    """Response HTML includes seeded player display names."""
    resp = await app_client.get("/stats/wingspan_in")
    text = resp.text
    assert "Tall Center" in text
    assert "Long Wing" in text
    assert "Quick Guard" in text


@pytest.mark.asyncio
async def test_metric_page_contains_table_headers(app_client, seed_data) -> None:
    """Response HTML has expected column headers."""
    resp = await app_client.get("/stats/wingspan_in")
    text = resp.text
    assert "Rank" in text
    assert "Player" in text
    assert "Pos" in text
    assert "Year" in text
    assert "Draft" in text
    assert "Pctl" in text
    assert "NBA Status" in text
    assert "Wingspan" in text


@pytest.mark.asyncio
async def test_metric_page_year_filter(app_client, seed_data) -> None:
    """Filtering by year=2024 shows only 2024 players."""
    resp = await app_client.get("/stats/wingspan_in?year=2024")
    text = resp.text
    assert "Tall Center" in text
    assert "Quick Guard" not in text  # 2025 player


@pytest.mark.asyncio
async def test_metric_page_position_filter(app_client, seed_data) -> None:
    """Filtering by position=PG shows only point guards."""
    resp = await app_client.get("/stats/wingspan_in?position=PG")
    text = resp.text
    assert "Quick Guard" in text
    assert "Tall Center" not in text  # C position


@pytest.mark.asyncio
async def test_metric_page_combined_filters(app_client, seed_data) -> None:
    """Year + position filter together."""
    resp = await app_client.get("/stats/wingspan_in?year=2025&position=PG")
    text = resp.text
    assert "Quick Guard" in text
    assert "Short Guard" in text
    assert "Tall Center" not in text


@pytest.mark.asyncio
async def test_metric_page_summary_cards_present(app_client, seed_data) -> None:
    """Response has Highest, Lowest, Typical summary cards."""
    resp = await app_client.get("/stats/wingspan_in")
    text = resp.text
    assert "Highest" in text
    assert "Lowest" in text
    assert "Typical" in text


@pytest.mark.asyncio
async def test_metric_page_breadcrumb(app_client, seed_data) -> None:
    """Response has breadcrumb with Stats link."""
    resp = await app_client.get("/stats/wingspan_in")
    assert '/stats"' in resp.text or "/stats'" in resp.text


@pytest.mark.asyncio
async def test_metric_page_filter_dropdowns(app_client, seed_data) -> None:
    """Response has metric, year, and position select elements."""
    resp = await app_client.get("/stats/wingspan_in")
    text = resp.text
    assert "<select" in text
    assert "All Years" in text
    assert "All Positions" in text



# === Service Tests ===


@pytest.mark.asyncio
async def test_get_leaderboard_returns_sorted_desc(db_session, seed_data) -> None:
    """Wingspan results are ordered highest first."""
    result = await get_leaderboard(db_session, "wingspan_in")
    values = [e.raw_value for e in result.entries]
    assert values == sorted(values, reverse=True)


@pytest.mark.asyncio
async def test_get_leaderboard_returns_sorted_asc_for_times(db_session, seed_data) -> None:
    """Lane agility results are ordered lowest (fastest) first."""
    result = await get_leaderboard(db_session, "lane_agility_time_s")
    values = [e.raw_value for e in result.entries]
    assert values == sorted(values)


@pytest.mark.asyncio
async def test_get_leaderboard_filters_nulls(db_session, seed_data) -> None:
    """Players without data for a metric are excluded."""
    result = await get_leaderboard(db_session, "lane_agility_time_s")
    # Only 2 players have agility data
    assert result.total == 2


@pytest.mark.asyncio
async def test_get_leaderboard_highest_lowest_typical(db_session, seed_data) -> None:
    """Result has correct highest, lowest, and typical (median) entries."""
    result = await get_leaderboard(db_session, "wingspan_in")
    assert result.highest is not None
    assert result.lowest is not None
    assert result.typical is not None
    assert result.highest.raw_value == 94.0  # Tall Center
    assert result.lowest.raw_value == 72.0  # Short Guard
    # Median of 5 sorted values [94, 90, 88, 78, 72] is index 2 = 88.0
    assert result.typical.raw_value == 88.0


@pytest.mark.asyncio
async def test_get_leaderboard_percentile_computation(db_session, seed_data) -> None:
    """Top player has high percentile, bottom has low."""
    result = await get_leaderboard(db_session, "wingspan_in")
    assert result.entries[0].percentile is not None
    assert result.entries[0].percentile >= 80.0
    assert result.entries[-1].percentile is not None
    assert result.entries[-1].percentile <= 20.0


@pytest.mark.asyncio
async def test_get_leaderboard_draft_info_populated(db_session, seed_data) -> None:
    """Entries include draft_pick and draft_round from PlayerMaster."""
    result = await get_leaderboard(db_session, "wingspan_in")
    tall_center = next(e for e in result.entries if e.display_name == "Tall Center")
    assert tall_center.draft_pick == 1
    assert tall_center.draft_round == 1

    no_draft = next(e for e in result.entries if e.display_name == "No Draft")
    assert no_draft.draft_pick is None


@pytest.mark.asyncio
async def test_get_leaderboard_nba_status_populated(db_session, seed_data) -> None:
    """Entries include is_active_nba from PlayerStatus."""
    result = await get_leaderboard(db_session, "wingspan_in")
    tall_center = next(e for e in result.entries if e.display_name == "Tall Center")
    assert tall_center.is_active_nba is True

    short_guard = next(e for e in result.entries if e.display_name == "Short Guard")
    assert short_guard.is_active_nba is False

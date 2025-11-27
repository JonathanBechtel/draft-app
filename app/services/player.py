"""
Player service module.

Business logic for player data aggregation and queries.
"""

from typing import Any, Optional
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_agility import CombineAgility


def calculate_age(birthdate: Optional[date]) -> Optional[int]:
    """Calculate age from birthdate."""
    if not birthdate:
        return None
    today = date.today()
    age = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        age -= 1
    return age


async def get_player_by_id(db: AsyncSession, player_id: int) -> Optional[PlayerMaster]:
    """
    Get a player by their ID.

    Args:
        db: Database session
        player_id: Player's unique identifier

    Returns:
        PlayerMaster instance or None if not found
    """
    stmt = select(PlayerMaster).where(
        PlayerMaster.id == player_id  # type: ignore[arg-type]
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_players_list(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    draft_year: Optional[int] = None,
    search: Optional[str] = None,
) -> list[PlayerMaster]:
    """
    Get a paginated list of players.

    Args:
        db: Database session
        limit: Maximum number of results
        offset: Number of results to skip
        draft_year: Filter by draft year
        search: Search term for player name

    Returns:
        List of PlayerMaster instances
    """
    query: Any = select(PlayerMaster)

    if draft_year:
        query = query.where(PlayerMaster.draft_year == draft_year)  # type: ignore[arg-type]

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            PlayerMaster.display_name.ilike(search_pattern)  # type: ignore[union-attr]
            | PlayerMaster.first_name.ilike(search_pattern)  # type: ignore[union-attr]
            | PlayerMaster.last_name.ilike(search_pattern)  # type: ignore[union-attr]
        )

    query = query.order_by(PlayerMaster.display_name).offset(offset).limit(limit)

    result = await db.execute(query)
    return list(result.scalars().all())


async def get_top_prospects(
    db: AsyncSession,
    limit: int = 6,
    draft_year: Optional[int] = None,
) -> list[dict]:
    """
    Get top prospects for display in the homepage grid.

    Returns player data with combine metrics for card display.

    Args:
        db: Database session
        limit: Maximum number of prospects to return
        draft_year: Filter by draft year (defaults to current)

    Returns:
        List of prospect dictionaries with player and combine data
    """
    # Build base query joining players with combine data
    query: Any = select(PlayerMaster, CombineAnthro).outerjoin(
        CombineAnthro,
        PlayerMaster.id == CombineAnthro.player_id,  # type: ignore[arg-type]
    )

    if draft_year:
        query = query.where(PlayerMaster.draft_year == draft_year)  # type: ignore[arg-type]

    # Order by draft_pick (for drafted players) or display_name
    query = query.order_by(
        PlayerMaster.draft_pick.nullslast(),  # type: ignore[union-attr]
        PlayerMaster.display_name,
    ).limit(limit)

    result = await db.execute(query)
    rows = result.all()

    prospects = []
    for player, anthro in rows:
        prospect = {
            "id": player.id,
            "display_name": player.display_name
            or f"{player.first_name} {player.last_name}",
            "position": anthro.raw_position if anthro else None,
            "school": player.school,
            "draft_year": player.draft_year,
            "draft_pick": player.draft_pick,
            "age": calculate_age(player.birthdate),
            "image_url": None,  # Placeholder - will use placehold.co in templates
            "measurables": {},
            "change": 0,  # Placeholder for rank change
        }

        if anthro:
            prospect["measurables"] = {
                "height": anthro.height_wo_shoes_in,
                "wingspan": anthro.wingspan_in,
                "standing_reach": anthro.standing_reach_in,
                "weight": anthro.weight_lb,
            }

        prospects.append(prospect)

    return prospects


async def get_player_detail(db: AsyncSession, player_id: int) -> Optional[dict]:
    """
    Get comprehensive player detail for the player page.

    Args:
        db: Database session
        player_id: Player's unique identifier

    Returns:
        Dictionary with player data, combine metrics, and related info
    """
    # Get player with combine data
    query: Any = (
        select(PlayerMaster, CombineAnthro, CombineAgility)
        .outerjoin(
            CombineAnthro,
            PlayerMaster.id == CombineAnthro.player_id,  # type: ignore[arg-type]
        )
        .outerjoin(
            CombineAgility,
            PlayerMaster.id == CombineAgility.player_id,  # type: ignore[arg-type]
        )
        .where(PlayerMaster.id == player_id)  # type: ignore[arg-type]
    )

    result = await db.execute(query)
    row = result.first()

    if not row:
        return None

    player, anthro, agility = row

    detail = {
        "id": player.id,
        "display_name": player.display_name
        or f"{player.first_name} {player.last_name}",
        "first_name": player.first_name,
        "last_name": player.last_name,
        "position": anthro.raw_position if anthro else None,
        "school": player.school,
        "high_school": player.high_school,
        "birthdate": player.birthdate,
        "age": calculate_age(player.birthdate),
        "birth_city": player.birth_city,
        "birth_state_province": player.birth_state_province,
        "birth_country": player.birth_country,
        "shoots": player.shoots,
        "draft_year": player.draft_year,
        "draft_round": player.draft_round,
        "draft_pick": player.draft_pick,
        "draft_team": player.draft_team,
        "image_url": None,  # Placeholder
        "anthropometrics": {},
        "agility": {},
    }

    if anthro:
        detail["anthropometrics"] = {
            "height_wo_shoes": anthro.height_wo_shoes_in,
            "height_w_shoes": anthro.height_w_shoes_in,
            "wingspan": anthro.wingspan_in,
            "standing_reach": anthro.standing_reach_in,
            "weight": anthro.weight_lb,
            "body_fat_pct": anthro.body_fat_pct,
            "hand_length": anthro.hand_length_in,
            "hand_width": anthro.hand_width_in,
        }

    if agility:
        detail["agility"] = {
            "lane_agility_time": agility.lane_agility_time_s,
            "shuttle_run": agility.shuttle_run_s,
            "three_quarter_sprint": agility.three_quarter_sprint_s,
            "standing_vertical": agility.standing_vertical_in,
            "max_vertical": agility.max_vertical_in,
            "bench_press_reps": agility.bench_press_reps,
        }

    return detail


async def search_players(
    db: AsyncSession,
    query_str: str,
    limit: int = 10,
) -> list[dict]:
    """
    Search players by name for autocomplete.

    Args:
        db: Database session
        query_str: Search term
        limit: Maximum number of results

    Returns:
        List of player summary dictionaries
    """
    search_pattern = f"%{query_str}%"

    stmt: Any = (
        select(PlayerMaster)
        .where(
            PlayerMaster.display_name.ilike(search_pattern)  # type: ignore[union-attr]
            | PlayerMaster.first_name.ilike(search_pattern)  # type: ignore[union-attr]
            | PlayerMaster.last_name.ilike(search_pattern)  # type: ignore[union-attr]
        )
        .order_by(PlayerMaster.display_name)
        .limit(limit)
    )

    result = await db.execute(stmt)
    players = result.scalars().all()

    return [
        {
            "id": p.id,
            "display_name": p.display_name or f"{p.first_name} {p.last_name}",
            "school": p.school,
            "draft_year": p.draft_year,
        }
        for p in players
    ]


async def get_players_for_comparison(
    db: AsyncSession,
    draft_year: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Get list of players available for head-to-head comparison.

    Args:
        db: Database session
        draft_year: Filter by draft year
        limit: Maximum number of players

    Returns:
        List of player summary dictionaries
    """
    query: Any = select(PlayerMaster)

    if draft_year:
        query = query.where(PlayerMaster.draft_year == draft_year)  # type: ignore[arg-type]

    query = query.order_by(
        PlayerMaster.draft_pick.nullslast(),  # type: ignore[union-attr]
        PlayerMaster.display_name,
    ).limit(limit)

    result = await db.execute(query)
    players = result.scalars().all()

    return [
        {
            "id": p.id,
            "display_name": p.display_name or f"{p.first_name} {p.last_name}",
            "school": p.school,
            "draft_year": p.draft_year,
        }
        for p in players
    ]

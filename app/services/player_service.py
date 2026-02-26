"""Player-related service functions."""

from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.players import PlayerProfileRead
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.schemas.seasons import Season
from app.services.image_assets_service import get_current_image_url_for_player
from app.utils.images import get_placeholder_url


async def get_player_profile_by_slug(
    db: AsyncSession,
    slug: str,
) -> Optional[PlayerProfileRead]:
    """Fetch player profile data by slug, joining related tables.

    Args:
        db: Async database session
        slug: Player's URL slug

    Returns:
        PlayerProfileRead if found, None otherwise
    """
    # Build query joining PlayerMaster -> PlayerStatus -> Position
    stmt = (
        select(
            PlayerMaster.id,
            PlayerMaster.slug,
            PlayerMaster.display_name,
            PlayerMaster.birthdate,
            PlayerMaster.birth_city,
            PlayerMaster.birth_state_province,
            PlayerMaster.birth_country,
            PlayerMaster.school,
            PlayerMaster.high_school,
            PlayerMaster.shoots,
            PlayerStatus.height_in,
            PlayerStatus.weight_lb,
            PlayerStatus.raw_position,
            Position.code.label("position_code"),  # type: ignore[attr-defined]
        )  # type: ignore[call-overload, misc]
        .select_from(PlayerMaster)
        .outerjoin(PlayerStatus, PlayerStatus.player_id == PlayerMaster.id)
        .outerjoin(Position, Position.id == PlayerStatus.position_id)
        .where(PlayerMaster.slug == slug)
    )

    result = await db.execute(stmt)
    row = result.mappings().first()

    if not row:
        return None

    player_id = row["id"]

    # Fetch most recent wingspan from CombineAnthro, ordered by season year
    # Join to Season table to ensure chronological ordering regardless of ID sequence
    wingspan_stmt = (
        select(CombineAnthro.wingspan_in, Season.start_year)  # type: ignore[call-overload]
        .join(Season, Season.id == CombineAnthro.season_id)
        .where(CombineAnthro.player_id == player_id)
        .where(CombineAnthro.wingspan_in.isnot(None))  # type: ignore[union-attr]
        .order_by(desc(Season.start_year))  # type: ignore[arg-type]
        .limit(1)
    )
    wingspan_result = await db.execute(wingspan_stmt)
    wingspan_row = wingspan_result.first()
    wingspan_value = wingspan_row[0] if wingspan_row else None
    combine_year = wingspan_row[1] if wingspan_row else None

    photo_url = await get_current_image_url_for_player(
        db,
        player_id=player_id,
        style="default",
    )
    if photo_url is None:
        photo_url = get_placeholder_url(row["display_name"], player_id=player_id)

    return PlayerProfileRead(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        birthdate=row["birthdate"],
        birth_city=row["birth_city"],
        birth_state_province=row["birth_state_province"],
        birth_country=row["birth_country"],
        school=row["school"],
        high_school=row["high_school"],
        shoots=row["shoots"],
        height_in=row["height_in"],
        weight_lb=row["weight_lb"],
        raw_position=row["raw_position"],
        position_code=row["position_code"],
        wingspan_in=wingspan_value,
        combine_year=combine_year,
        photo_url=photo_url,
    )

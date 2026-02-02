"""Admin player service for CRUD operations.

Handles business logic for player management: queries, validation, parsing,
and database operations. Routes should be thin wrappers around these functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.position_taxonomy import derive_position_tags
from app.schemas.news_items import NewsItem
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position


@dataclass
class PlayerWithStatus:
    """Player with optional status data for list views."""

    player: PlayerMaster
    is_active_nba: bool | None = None
    current_team: str | None = None


@dataclass
class PlayerListResult:
    """Result of a paginated player list query."""

    players: list[PlayerWithStatus]
    total: int
    draft_years: list[int]


@dataclass
class PlayerFormData:
    """Raw form data from request (all strings)."""

    display_name: str
    first_name: str
    last_name: str
    prefix: str | None = None
    middle_name: str | None = None
    suffix: str | None = None
    birthdate: str | None = None
    birth_city: str | None = None
    birth_state_province: str | None = None
    birth_country: str | None = None
    school: str | None = None
    high_school: str | None = None
    shoots: str | None = None
    draft_year: str | None = None
    draft_round: str | None = None
    draft_pick: str | None = None
    draft_team: str | None = None
    nba_debut_date: str | None = None
    nba_debut_season: str | None = None
    reference_image_url: str | None = None


@dataclass
class ParsedPlayerData:
    """Validated and parsed player data ready for DB operations."""

    display_name: str
    first_name: str
    last_name: str
    prefix: str | None = None
    middle_name: str | None = None
    suffix: str | None = None
    birthdate: date | None = None
    birth_city: str | None = None
    birth_state_province: str | None = None
    birth_country: str | None = None
    school: str | None = None
    high_school: str | None = None
    shoots: str | None = None
    draft_year: int | None = None
    draft_round: int | None = None
    draft_pick: int | None = None
    draft_team: str | None = None
    nba_debut_date: date | None = None
    nba_debut_season: str | None = None
    reference_image_url: str | None = None


def _clean_str(val: str | None) -> str | None:
    """Clean optional string field, returning None for empty strings."""
    if val and val.strip():
        return val.strip()
    return None


async def list_players(
    db: AsyncSession,
    q: str | None,
    draft_year: int | None,
    position: str | None,
    nba_status: str | None,
    limit: int,
    offset: int,
) -> PlayerListResult:
    """List players with filters and pagination.

    Args:
        db: Async database session
        q: Search query (matches display_name, first_name, last_name, school)
        draft_year: Filter by draft year
        position: Filter by position (stored in shoots field)
        nba_status: Filter by NBA status ("active", "inactive", "unknown")
        limit: Maximum results to return
        offset: Number of results to skip

    Returns:
        PlayerListResult with players, total count, and available draft years
    """
    # Select player with status fields via outer join
    query = (
        select(  # type: ignore[call-overload]
            PlayerMaster,
            PlayerStatus.is_active_nba,
            PlayerStatus.current_team,
        )
        .outerjoin(PlayerStatus, PlayerStatus.player_id == PlayerMaster.id)
        .order_by(PlayerMaster.display_name)  # type: ignore[arg-type]
    )
    # Count query also needs the join for status filtering
    count_query = (
        select(func.count(PlayerMaster.id))  # type: ignore[arg-type]
        .select_from(PlayerMaster)
        .outerjoin(
            PlayerStatus,
            PlayerStatus.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
    )

    # Apply search filter
    if q and q.strip():
        search_term = f"%{q.strip()}%"
        search_filter = or_(
            PlayerMaster.display_name.ilike(search_term),  # type: ignore[union-attr]
            PlayerMaster.first_name.ilike(search_term),  # type: ignore[union-attr]
            PlayerMaster.last_name.ilike(search_term),  # type: ignore[union-attr]
            PlayerMaster.school.ilike(search_term),  # type: ignore[union-attr]
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # Apply draft year filter
    if draft_year is not None:
        query = query.where(
            PlayerMaster.draft_year == draft_year  # type: ignore[arg-type]
        )
        count_query = count_query.where(
            PlayerMaster.draft_year == draft_year  # type: ignore[arg-type]
        )

    # Apply position filter (stored in shoots field)
    if position and position.strip():
        query = query.where(
            PlayerMaster.shoots == position  # type: ignore[arg-type]
        )
        count_query = count_query.where(
            PlayerMaster.shoots == position  # type: ignore[arg-type]
        )

    # Apply NBA status filter
    if nba_status and nba_status.strip():
        status_val = nba_status.strip().lower()
        if status_val == "active":
            query = query.where(
                PlayerStatus.is_active_nba.is_(True)  # type: ignore[union-attr]
            )
            count_query = count_query.where(
                PlayerStatus.is_active_nba.is_(True)  # type: ignore[union-attr]
            )
        elif status_val == "inactive":
            query = query.where(
                PlayerStatus.is_active_nba.is_(False)  # type: ignore[union-attr]
            )
            count_query = count_query.where(
                PlayerStatus.is_active_nba.is_(False)  # type: ignore[union-attr]
            )
        elif status_val == "unknown":
            # Unknown = no status record OR is_active_nba is NULL
            query = query.where(
                or_(
                    PlayerStatus.id.is_(None),  # type: ignore[union-attr]
                    PlayerStatus.is_active_nba.is_(None),  # type: ignore[union-attr]
                )
            )
            count_query = count_query.where(
                or_(
                    PlayerStatus.id.is_(None),  # type: ignore[union-attr]
                    PlayerStatus.is_active_nba.is_(None),  # type: ignore[union-attr]
                )
            )

    # Get total count
    total = await db.scalar(count_query)
    total = total or 0

    # Apply pagination
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    rows = result.all()

    # Build PlayerWithStatus objects from joined results
    players = [
        PlayerWithStatus(
            player=row[0],
            is_active_nba=row[1],
            current_team=row[2],
        )
        for row in rows
    ]

    # Get distinct draft years for filter dropdown
    years_result = await db.execute(
        select(PlayerMaster.draft_year)  # type: ignore[call-overload]
        .where(PlayerMaster.draft_year.isnot(None))  # type: ignore[union-attr]
        .distinct()
        .order_by(PlayerMaster.draft_year.desc())  # type: ignore[union-attr]
    )
    draft_years = [y for y in years_result.scalars().all() if y is not None]

    return PlayerListResult(players=players, total=total, draft_years=draft_years)


async def get_player_by_id(db: AsyncSession, player_id: int) -> PlayerMaster | None:
    """Fetch a player by ID.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        PlayerMaster if found, None otherwise
    """
    result = await db.execute(
        select(PlayerMaster).where(PlayerMaster.id == player_id)  # type: ignore[arg-type]
    )
    return result.scalar_one_or_none()


def validate_player_form(data: PlayerFormData) -> str | None:
    """Validate required fields in player form data.

    Args:
        data: Raw form data

    Returns:
        Error message if validation fails, None if valid
    """
    if not data.display_name or not data.display_name.strip():
        return "Display name is required."

    if not data.first_name or not data.first_name.strip():
        return "First name is required."

    if not data.last_name or not data.last_name.strip():
        return "Last name is required."

    return None


def parse_player_form(data: PlayerFormData) -> ParsedPlayerData | str:
    """Parse and validate player form data, converting strings to typed values.

    Args:
        data: Raw form data

    Returns:
        ParsedPlayerData if parsing succeeds, error message string if it fails
    """
    # Parse birthdate
    parsed_birthdate: date | None = None
    if data.birthdate and data.birthdate.strip():
        try:
            parsed_birthdate = date.fromisoformat(data.birthdate.strip())
        except ValueError:
            return "Invalid birthdate format. Use YYYY-MM-DD."

    # Parse NBA debut date
    parsed_nba_debut_date: date | None = None
    if data.nba_debut_date and data.nba_debut_date.strip():
        try:
            parsed_nba_debut_date = date.fromisoformat(data.nba_debut_date.strip())
        except ValueError:
            return "Invalid NBA debut date format. Use YYYY-MM-DD."

    # Parse draft year
    parsed_draft_year: int | None = None
    if data.draft_year and data.draft_year.strip():
        try:
            parsed_draft_year = int(data.draft_year.strip())
        except ValueError:
            return "Draft year must be a number."

    # Parse draft round
    parsed_draft_round: int | None = None
    if data.draft_round and data.draft_round.strip():
        try:
            parsed_draft_round = int(data.draft_round.strip())
        except ValueError:
            return "Draft round must be a number."

    # Parse draft pick
    parsed_draft_pick: int | None = None
    if data.draft_pick and data.draft_pick.strip():
        try:
            parsed_draft_pick = int(data.draft_pick.strip())
        except ValueError:
            return "Draft pick must be a number."

    return ParsedPlayerData(
        display_name=data.display_name.strip(),
        first_name=data.first_name.strip(),
        last_name=data.last_name.strip(),
        prefix=_clean_str(data.prefix),
        middle_name=_clean_str(data.middle_name),
        suffix=_clean_str(data.suffix),
        birthdate=parsed_birthdate,
        birth_city=_clean_str(data.birth_city),
        birth_state_province=_clean_str(data.birth_state_province),
        birth_country=_clean_str(data.birth_country),
        school=_clean_str(data.school),
        high_school=_clean_str(data.high_school),
        shoots=_clean_str(data.shoots),
        draft_year=parsed_draft_year,
        draft_round=parsed_draft_round,
        draft_pick=parsed_draft_pick,
        draft_team=_clean_str(data.draft_team),
        nba_debut_date=parsed_nba_debut_date,
        nba_debut_season=_clean_str(data.nba_debut_season),
        reference_image_url=_clean_str(data.reference_image_url),
    )


async def create_player(db: AsyncSession, data: ParsedPlayerData) -> PlayerMaster:
    """Create a new player in the database.

    Args:
        db: Async database session
        data: Parsed and validated player data

    Returns:
        The created PlayerMaster instance
    """
    player = PlayerMaster(
        display_name=data.display_name,
        first_name=data.first_name,
        last_name=data.last_name,
        prefix=data.prefix,
        middle_name=data.middle_name,
        suffix=data.suffix,
        birthdate=data.birthdate,
        birth_city=data.birth_city,
        birth_state_province=data.birth_state_province,
        birth_country=data.birth_country,
        school=data.school,
        high_school=data.high_school,
        shoots=data.shoots,
        draft_year=data.draft_year,
        draft_round=data.draft_round,
        draft_pick=data.draft_pick,
        draft_team=data.draft_team,
        nba_debut_date=data.nba_debut_date,
        nba_debut_season=data.nba_debut_season,
        reference_image_url=data.reference_image_url,
    )
    db.add(player)
    await db.flush()
    return player


async def update_player(
    db: AsyncSession,
    player: PlayerMaster,
    data: ParsedPlayerData,
) -> PlayerMaster:
    """Update an existing player in the database.

    Args:
        db: Async database session
        player: Existing PlayerMaster instance to update
        data: Parsed and validated player data

    Returns:
        The updated PlayerMaster instance
    """
    player.display_name = data.display_name
    player.first_name = data.first_name
    player.last_name = data.last_name
    player.prefix = data.prefix
    player.middle_name = data.middle_name
    player.suffix = data.suffix
    player.birthdate = data.birthdate
    player.birth_city = data.birth_city
    player.birth_state_province = data.birth_state_province
    player.birth_country = data.birth_country
    player.school = data.school
    player.high_school = data.high_school
    player.shoots = data.shoots
    player.draft_year = data.draft_year
    player.draft_round = data.draft_round
    player.draft_pick = data.draft_pick
    player.draft_team = data.draft_team
    player.nba_debut_date = data.nba_debut_date
    player.nba_debut_season = data.nba_debut_season
    player.reference_image_url = data.reference_image_url
    player.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await db.flush()
    return player


async def can_delete_player(
    db: AsyncSession, player_id: int
) -> tuple[bool, str | None]:
    """Check if a player can be deleted (has no linked news items).

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        Tuple of (can_delete, error_message). error_message is None if can_delete is True.
    """
    news_count_result = await db.execute(
        select(func.count()).where(NewsItem.player_id == player_id)  # type: ignore[arg-type]
    )
    news_count = news_count_result.scalar_one()

    if news_count > 0:
        return (
            False,
            f"it has {news_count} linked news item(s). Unlink the news items first.",
        )

    return (True, None)


async def delete_player(db: AsyncSession, player: PlayerMaster) -> None:
    """Delete a player from the database.

    Args:
        db: Async database session
        player: PlayerMaster instance to delete
    """
    await db.delete(player)
    await db.flush()


async def get_player_status_by_player_id(
    db: AsyncSession, player_id: int
) -> PlayerStatus | None:
    """Fetch a player's status record by player ID.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        PlayerStatus if found, None otherwise
    """
    result = await db.execute(
        select(PlayerStatus).where(
            PlayerStatus.player_id == player_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


@dataclass
class PlayerStatusFormData:
    """Raw form data for player status fields."""

    is_active_nba: str | None = None  # "true", "false", "" (unknown)
    current_team: str | None = None
    nba_last_season: str | None = None
    raw_position: str | None = None
    height_in: str | None = None  # needs int parsing
    weight_lb: str | None = None  # needs int parsing


def _parse_bool_field(val: str | None) -> bool | None:
    """Parse a boolean form field. Empty or None returns None (unknown)."""
    if not val or not val.strip():
        return None
    lower = val.strip().lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    return None


def _parse_int_field(val: str | None) -> int | None:
    """Parse an integer form field. Empty or None returns None."""
    if not val or not val.strip():
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


async def _resolve_position_id(db: AsyncSession, raw_position: str) -> int | None:
    """Derive position_id from raw_position string using taxonomy.

    Args:
        db: Async database session
        raw_position: Raw position string like "PG", "SF-PF"

    Returns:
        Position ID if derivable, None otherwise
    """
    fine_code, parents = derive_position_tags(raw_position)
    if not fine_code:
        return None

    # Look up existing position by code
    result = await db.execute(
        select(Position).where(Position.code == fine_code)  # type: ignore[arg-type]
    )
    position = result.scalar_one_or_none()

    if position is None:
        # Create new position record
        position = Position(code=fine_code, parents=parents)
        db.add(position)
        await db.flush()

    return position.id


async def update_player_status(
    db: AsyncSession,
    player_id: int,
    data: PlayerStatusFormData,
) -> PlayerStatus:
    """Create or update PlayerStatus for a player.

    Args:
        db: Async database session
        player_id: Player's database ID
        data: Form data for status fields

    Returns:
        The created or updated PlayerStatus instance
    """
    # Fetch existing or create new
    status = await get_player_status_by_player_id(db, player_id)
    if status is None:
        status = PlayerStatus(player_id=player_id)
        db.add(status)

    # Parse and set fields
    status.is_active_nba = _parse_bool_field(data.is_active_nba)
    status.current_team = _clean_str(data.current_team)
    status.nba_last_season = _clean_str(data.nba_last_season)
    status.raw_position = _clean_str(data.raw_position)
    status.height_in = _parse_int_field(data.height_in)
    status.weight_lb = _parse_int_field(data.weight_lb)

    # Auto-derive position_id from raw_position
    if status.raw_position:
        status.position_id = await _resolve_position_id(db, status.raw_position)
    else:
        status.position_id = None

    # Set source to 'manual' for admin edits
    status.source = "manual"
    status.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await db.flush()
    return status

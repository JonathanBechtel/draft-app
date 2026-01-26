"""Admin service for player-related sub-tables.

Handles business logic for PlayerStatus, PlayerAlias, and PlayerExternalId
CRUD operations. Routes should be thin wrappers around these functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.schemas.positions import Position


# =============================================================================
# Form Data Classes
# =============================================================================


@dataclass
class PlayerStatusFormData:
    """Raw form data for player status (all strings)."""

    position_id: str | None = None
    is_active_nba: str | None = None  # checkbox value
    current_team: str | None = None
    nba_last_season: str | None = None
    raw_position: str | None = None
    height_in: str | None = None
    weight_lb: str | None = None
    source: str | None = None


@dataclass
class PlayerAliasFormData:
    """Raw form data for player alias (all strings)."""

    full_name: str  # required
    prefix: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    suffix: str | None = None
    context: str | None = None


@dataclass
class PlayerExternalIdFormData:
    """Raw form data for player external ID (all strings)."""

    system: str  # required
    external_id: str  # required
    source_url: str | None = None


# =============================================================================
# Validation Result Classes
# =============================================================================


@dataclass
class ParsedStatusData:
    """Validated and parsed player status data."""

    position_id: int | None = None
    is_active_nba: bool | None = None
    current_team: str | None = None
    nba_last_season: str | None = None
    raw_position: str | None = None
    height_in: int | None = None
    weight_lb: int | None = None
    source: str | None = None


@dataclass
class ParsedAliasData:
    """Validated and parsed player alias data."""

    full_name: str
    prefix: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    suffix: str | None = None
    context: str | None = None


@dataclass
class ParsedExternalIdData:
    """Validated and parsed player external ID data."""

    system: str
    external_id: str
    source_url: str | None = None


# =============================================================================
# Helper Functions
# =============================================================================


def _clean_str(val: str | None) -> str | None:
    """Clean optional string field, returning None for empty strings."""
    if val and val.strip():
        return val.strip()
    return None


def _parse_int(
    val: str | None, min_val: int | None = None, max_val: int | None = None
) -> int | None:
    """Parse optional integer with optional range validation."""
    if not val or not val.strip():
        return None
    try:
        parsed = int(val.strip())
        if min_val is not None and parsed < min_val:
            return None
        if max_val is not None and parsed > max_val:
            return None
        return parsed
    except ValueError:
        return None


# =============================================================================
# Position Queries
# =============================================================================


async def get_all_positions(db: AsyncSession) -> list[Position]:
    """Fetch all positions for dropdown.

    Args:
        db: Async database session

    Returns:
        List of Position objects ordered by code
    """
    result = await db.execute(
        select(Position).order_by(Position.code)  # type: ignore[arg-type]
    )
    return list(result.scalars().all())


# =============================================================================
# PlayerStatus CRUD
# =============================================================================


async def get_player_status(db: AsyncSession, player_id: int) -> PlayerStatus | None:
    """Fetch the status record for a player.

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


def validate_status_form(data: PlayerStatusFormData) -> str | None:
    """Validate player status form data.

    Args:
        data: Raw form data

    Returns:
        Error message if validation fails, None if valid
    """
    # Validate height_in range if provided
    if data.height_in and data.height_in.strip():
        try:
            height = int(data.height_in.strip())
            if height < 60 or height > 96:
                return "Height must be between 60 and 96 inches."
        except ValueError:
            return "Height must be a valid number."

    # Validate weight_lb range if provided
    if data.weight_lb and data.weight_lb.strip():
        try:
            weight = int(data.weight_lb.strip())
            if weight < 100 or weight > 400:
                return "Weight must be between 100 and 400 pounds."
        except ValueError:
            return "Weight must be a valid number."

    return None


def parse_status_form(data: PlayerStatusFormData) -> ParsedStatusData | str:
    """Parse and validate player status form data.

    Args:
        data: Raw form data

    Returns:
        ParsedStatusData if parsing succeeds, error message string if it fails
    """
    # Parse position_id
    position_id = _parse_int(data.position_id)

    # Parse is_active_nba (checkbox with hidden input fallback)
    is_active_nba: bool | None = None
    if data.is_active_nba and data.is_active_nba.strip():
        val = data.is_active_nba.strip().lower()
        if val in ("true", "on", "1", "yes"):
            is_active_nba = True
        elif val in ("false", "off", "0", "no"):
            is_active_nba = False

    # Parse height_in
    height_in = _parse_int(data.height_in, min_val=60, max_val=96)
    if data.height_in and data.height_in.strip() and height_in is None:
        return "Height must be between 60 and 96 inches."

    # Parse weight_lb
    weight_lb = _parse_int(data.weight_lb, min_val=100, max_val=400)
    if data.weight_lb and data.weight_lb.strip() and weight_lb is None:
        return "Weight must be between 100 and 400 pounds."

    return ParsedStatusData(
        position_id=position_id,
        is_active_nba=is_active_nba,
        current_team=_clean_str(data.current_team),
        nba_last_season=_clean_str(data.nba_last_season),
        raw_position=_clean_str(data.raw_position),
        height_in=height_in,
        weight_lb=weight_lb,
        source=_clean_str(data.source),
    )


async def upsert_player_status(
    db: AsyncSession,
    player_id: int,
    data: ParsedStatusData,
) -> PlayerStatus:
    """Create or update a player status record.

    Args:
        db: Async database session
        player_id: Player's database ID
        data: Parsed and validated status data

    Returns:
        The created or updated PlayerStatus instance
    """
    status = await get_player_status(db, player_id)

    if status is None:
        status = PlayerStatus(
            player_id=player_id,
            position_id=data.position_id,
            is_active_nba=data.is_active_nba,
            current_team=data.current_team,
            nba_last_season=data.nba_last_season,
            raw_position=data.raw_position,
            height_in=data.height_in,
            weight_lb=data.weight_lb,
            source=data.source,
        )
        db.add(status)
    else:
        status.position_id = data.position_id
        status.is_active_nba = data.is_active_nba
        status.current_team = data.current_team
        status.nba_last_season = data.nba_last_season
        status.raw_position = data.raw_position
        status.height_in = data.height_in
        status.weight_lb = data.weight_lb
        status.source = data.source
        status.updated_at = datetime.utcnow()

    await db.flush()
    return status


async def delete_player_status(db: AsyncSession, status: PlayerStatus) -> None:
    """Delete a player status record.

    Args:
        db: Async database session
        status: PlayerStatus instance to delete
    """
    await db.delete(status)
    await db.flush()


# =============================================================================
# PlayerAlias CRUD
# =============================================================================


async def list_player_aliases(db: AsyncSession, player_id: int) -> list[PlayerAlias]:
    """List all aliases for a player.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        List of PlayerAlias objects ordered by full_name
    """
    result = await db.execute(
        select(PlayerAlias)
        .where(PlayerAlias.player_id == player_id)  # type: ignore[arg-type]
        .order_by(PlayerAlias.full_name)  # type: ignore[arg-type]
    )
    return list(result.scalars().all())


async def get_player_alias_by_id(db: AsyncSession, alias_id: int) -> PlayerAlias | None:
    """Fetch an alias by ID.

    Args:
        db: Async database session
        alias_id: Alias database ID

    Returns:
        PlayerAlias if found, None otherwise
    """
    result = await db.execute(
        select(PlayerAlias).where(PlayerAlias.id == alias_id)  # type: ignore[arg-type]
    )
    return result.scalar_one_or_none()


async def count_player_aliases(db: AsyncSession, player_id: int) -> int:
    """Count aliases for a player.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        Count of alias records
    """
    result = await db.scalar(
        select(func.count()).where(
            PlayerAlias.player_id == player_id  # type: ignore[arg-type]
        )
    )
    return result or 0


def validate_alias_form(data: PlayerAliasFormData) -> str | None:
    """Validate player alias form data.

    Args:
        data: Raw form data

    Returns:
        Error message if validation fails, None if valid
    """
    if not data.full_name or not data.full_name.strip():
        return "Full name is required."
    return None


def parse_alias_form(data: PlayerAliasFormData) -> ParsedAliasData | str:
    """Parse and validate player alias form data.

    Args:
        data: Raw form data

    Returns:
        ParsedAliasData if parsing succeeds, error message string if it fails
    """
    if not data.full_name or not data.full_name.strip():
        return "Full name is required."

    return ParsedAliasData(
        full_name=data.full_name.strip(),
        prefix=_clean_str(data.prefix),
        first_name=_clean_str(data.first_name),
        middle_name=_clean_str(data.middle_name),
        last_name=_clean_str(data.last_name),
        suffix=_clean_str(data.suffix),
        context=_clean_str(data.context),
    )


async def check_alias_uniqueness(
    db: AsyncSession,
    player_id: int,
    full_name: str,
    exclude_id: int | None = None,
) -> bool:
    """Check if an alias full_name is unique for the player.

    Args:
        db: Async database session
        player_id: Player's database ID
        full_name: Full name to check
        exclude_id: Optional alias ID to exclude (for updates)

    Returns:
        True if unique, False if duplicate exists
    """
    query = select(func.count()).where(
        PlayerAlias.player_id == player_id,  # type: ignore[arg-type]
        PlayerAlias.full_name == full_name,  # type: ignore[arg-type]
    )
    if exclude_id is not None:
        query = query.where(PlayerAlias.id != exclude_id)  # type: ignore[arg-type]

    result = await db.scalar(query)
    return (result or 0) == 0


async def create_player_alias(
    db: AsyncSession,
    player_id: int,
    data: ParsedAliasData,
) -> PlayerAlias:
    """Create a new player alias.

    Args:
        db: Async database session
        player_id: Player's database ID
        data: Parsed and validated alias data

    Returns:
        The created PlayerAlias instance
    """
    alias = PlayerAlias(
        player_id=player_id,
        full_name=data.full_name,
        prefix=data.prefix,
        first_name=data.first_name,
        middle_name=data.middle_name,
        last_name=data.last_name,
        suffix=data.suffix,
        context=data.context,
    )
    db.add(alias)
    await db.flush()
    return alias


async def update_player_alias(
    db: AsyncSession,
    alias: PlayerAlias,
    data: ParsedAliasData,
) -> PlayerAlias:
    """Update an existing player alias.

    Args:
        db: Async database session
        alias: Existing PlayerAlias instance to update
        data: Parsed and validated alias data

    Returns:
        The updated PlayerAlias instance
    """
    alias.full_name = data.full_name
    alias.prefix = data.prefix
    alias.first_name = data.first_name
    alias.middle_name = data.middle_name
    alias.last_name = data.last_name
    alias.suffix = data.suffix
    alias.context = data.context
    await db.flush()
    return alias


async def delete_player_alias(db: AsyncSession, alias: PlayerAlias) -> None:
    """Delete a player alias.

    Args:
        db: Async database session
        alias: PlayerAlias instance to delete
    """
    await db.delete(alias)
    await db.flush()


# =============================================================================
# PlayerExternalId CRUD
# =============================================================================


async def list_player_external_ids(
    db: AsyncSession, player_id: int
) -> list[PlayerExternalId]:
    """List all external IDs for a player.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        List of PlayerExternalId objects ordered by system
    """
    result = await db.execute(
        select(PlayerExternalId)
        .where(PlayerExternalId.player_id == player_id)  # type: ignore[arg-type]
        .order_by(PlayerExternalId.system)  # type: ignore[arg-type]
    )
    return list(result.scalars().all())


async def get_player_external_id_by_id(
    db: AsyncSession, ext_id: int
) -> PlayerExternalId | None:
    """Fetch an external ID by ID.

    Args:
        db: Async database session
        ext_id: External ID database ID

    Returns:
        PlayerExternalId if found, None otherwise
    """
    result = await db.execute(
        select(PlayerExternalId).where(
            PlayerExternalId.id == ext_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


async def count_player_external_ids(db: AsyncSession, player_id: int) -> int:
    """Count external IDs for a player.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        Count of external ID records
    """
    result = await db.scalar(
        select(func.count()).where(
            PlayerExternalId.player_id == player_id  # type: ignore[arg-type]
        )
    )
    return result or 0


def validate_external_id_form(data: PlayerExternalIdFormData) -> str | None:
    """Validate player external ID form data.

    Args:
        data: Raw form data

    Returns:
        Error message if validation fails, None if valid
    """
    if not data.system or not data.system.strip():
        return "System is required."
    if not data.external_id or not data.external_id.strip():
        return "External ID is required."
    return None


def parse_external_id_form(
    data: PlayerExternalIdFormData,
) -> ParsedExternalIdData | str:
    """Parse and validate player external ID form data.

    Args:
        data: Raw form data

    Returns:
        ParsedExternalIdData if parsing succeeds, error message string if it fails
    """
    if not data.system or not data.system.strip():
        return "System is required."
    if not data.external_id or not data.external_id.strip():
        return "External ID is required."

    return ParsedExternalIdData(
        system=data.system.strip(),
        external_id=data.external_id.strip(),
        source_url=_clean_str(data.source_url),
    )


async def check_external_id_uniqueness(
    db: AsyncSession,
    system: str,
    external_id: str,
    exclude_id: int | None = None,
) -> bool:
    """Check if a system/external_id combination is globally unique.

    Args:
        db: Async database session
        system: System identifier
        external_id: External ID value
        exclude_id: Optional record ID to exclude (for updates)

    Returns:
        True if unique, False if duplicate exists
    """
    query = select(func.count()).where(
        PlayerExternalId.system == system,  # type: ignore[arg-type]
        PlayerExternalId.external_id == external_id,  # type: ignore[arg-type]
    )
    if exclude_id is not None:
        query = query.where(PlayerExternalId.id != exclude_id)  # type: ignore[arg-type]

    result = await db.scalar(query)
    return (result or 0) == 0


async def create_player_external_id(
    db: AsyncSession,
    player_id: int,
    data: ParsedExternalIdData,
) -> PlayerExternalId:
    """Create a new player external ID.

    Args:
        db: Async database session
        player_id: Player's database ID
        data: Parsed and validated external ID data

    Returns:
        The created PlayerExternalId instance
    """
    ext_id = PlayerExternalId(
        player_id=player_id,
        system=data.system,
        external_id=data.external_id,
        source_url=data.source_url,
    )
    db.add(ext_id)
    await db.flush()
    return ext_id


async def update_player_external_id(
    db: AsyncSession,
    ext: PlayerExternalId,
    data: ParsedExternalIdData,
) -> PlayerExternalId:
    """Update an existing player external ID.

    Args:
        db: Async database session
        ext: Existing PlayerExternalId instance to update
        data: Parsed and validated external ID data

    Returns:
        The updated PlayerExternalId instance
    """
    ext.system = data.system
    ext.external_id = data.external_id
    ext.source_url = data.source_url
    await db.flush()
    return ext


async def delete_player_external_id(db: AsyncSession, ext: PlayerExternalId) -> None:
    """Delete a player external ID.

    Args:
        db: Async database session
        ext: PlayerExternalId instance to delete
    """
    await db.delete(ext)
    await db.flush()

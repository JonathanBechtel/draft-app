"""Admin player service for CRUD operations.

Handles business logic for player management: queries, validation, parsing,
and database operations. Routes should be thin wrappers around these functions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting
from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot
from app.schemas.metrics import PlayerMetricValue, PlayerSimilarity
from app.schemas.news_items import NewsItem
from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster

logger = logging.getLogger(__name__)


@dataclass
class PlayerListResult:
    """Result of a paginated player list query."""

    players: list[PlayerMaster]
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


@dataclass
class PlayerDependencies:
    """Counts of records in tables that depend on a player.

    Used to check whether a player can be safely deleted.
    """

    # Identity & Status
    player_status: int = 0
    player_aliases: int = 0
    player_external_ids: int = 0
    player_bio_snapshots: int = 0

    # Combine Data
    combine_agility: int = 0
    combine_anthro: int = 0
    combine_shooting: int = 0

    # Analytics
    player_metric_values: int = 0
    player_similarity_anchor: int = 0
    player_similarity_comparison: int = 0

    # Content
    news_items: int = 0
    image_assets: int = 0

    @property
    def total(self) -> int:
        """Total count of all dependencies."""
        return (
            self.player_status
            + self.player_aliases
            + self.player_external_ids
            + self.player_bio_snapshots
            + self.combine_agility
            + self.combine_anthro
            + self.combine_shooting
            + self.player_metric_values
            + self.player_similarity_anchor
            + self.player_similarity_comparison
            + self.news_items
            + self.image_assets
        )

    @property
    def has_any(self) -> bool:
        """True if any dependencies exist."""
        return self.total > 0


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
    limit: int,
    offset: int,
) -> PlayerListResult:
    """List players with filters and pagination.

    Args:
        db: Async database session
        q: Search query (matches display_name, first_name, last_name, school)
        draft_year: Filter by draft year
        position: Filter by position (stored in shoots field)
        limit: Maximum results to return
        offset: Number of results to skip

    Returns:
        PlayerListResult with players, total count, and available draft years
    """
    query = select(PlayerMaster).order_by(
        PlayerMaster.display_name  # type: ignore[arg-type]
    )
    count_query = select(func.count(PlayerMaster.id))  # type: ignore[arg-type]

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

    # Get total count
    total = await db.scalar(count_query)
    total = total or 0

    # Apply pagination
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    players = list(result.scalars().all())

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
    player.updated_at = datetime.utcnow()
    await db.flush()
    return player


async def get_player_dependencies(
    db: AsyncSession, player_id: int
) -> PlayerDependencies:
    """Query all tables that reference a player and return dependency counts.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        PlayerDependencies with counts from all dependent tables
    """
    # Run all count queries in parallel-ish (they're all simple counts)
    status_count = await db.scalar(
        select(func.count()).where(
            PlayerStatus.player_id == player_id  # type: ignore[arg-type]
        )
    )
    aliases_count = await db.scalar(
        select(func.count()).where(
            PlayerAlias.player_id == player_id  # type: ignore[arg-type]
        )
    )
    external_ids_count = await db.scalar(
        select(func.count()).where(
            PlayerExternalId.player_id == player_id  # type: ignore[arg-type]
        )
    )
    bio_snapshots_count = await db.scalar(
        select(func.count()).where(
            PlayerBioSnapshot.player_id == player_id  # type: ignore[arg-type]
        )
    )
    agility_count = await db.scalar(
        select(func.count()).where(
            CombineAgility.player_id == player_id  # type: ignore[arg-type]
        )
    )
    anthro_count = await db.scalar(
        select(func.count()).where(
            CombineAnthro.player_id == player_id  # type: ignore[arg-type]
        )
    )
    shooting_count = await db.scalar(
        select(func.count()).where(
            CombineShooting.player_id == player_id  # type: ignore[arg-type]
        )
    )
    metric_values_count = await db.scalar(
        select(func.count()).where(
            PlayerMetricValue.player_id == player_id  # type: ignore[arg-type]
        )
    )
    similarity_anchor_count = await db.scalar(
        select(func.count()).where(
            PlayerSimilarity.anchor_player_id == player_id  # type: ignore[arg-type]
        )
    )
    similarity_comparison_count = await db.scalar(
        select(func.count()).where(
            PlayerSimilarity.comparison_player_id == player_id  # type: ignore[arg-type]
        )
    )
    news_count = await db.scalar(
        select(func.count()).where(
            NewsItem.player_id == player_id  # type: ignore[arg-type]
        )
    )
    image_assets_count = await db.scalar(
        select(func.count()).where(
            PlayerImageAsset.player_id == player_id  # type: ignore[arg-type]
        )
    )

    return PlayerDependencies(
        player_status=status_count or 0,
        player_aliases=aliases_count or 0,
        player_external_ids=external_ids_count or 0,
        player_bio_snapshots=bio_snapshots_count or 0,
        combine_agility=agility_count or 0,
        combine_anthro=anthro_count or 0,
        combine_shooting=shooting_count or 0,
        player_metric_values=metric_values_count or 0,
        player_similarity_anchor=similarity_anchor_count or 0,
        player_similarity_comparison=similarity_comparison_count or 0,
        news_items=news_count or 0,
        image_assets=image_assets_count or 0,
    )


async def can_delete_player(
    db: AsyncSession, player_id: int
) -> tuple[bool, PlayerDependencies]:
    """Check if a player can be deleted (has no dependencies).

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        Tuple of (can_delete, dependencies). can_delete is True if no dependencies exist.
    """
    deps = await get_player_dependencies(db, player_id)
    return (not deps.has_any, deps)


async def delete_player(db: AsyncSession, player: PlayerMaster) -> None:
    """Delete a player from the database.

    Args:
        db: Async database session
        player: PlayerMaster instance to delete
    """
    await db.delete(player)
    await db.flush()


@dataclass
class ImageValidationResult:
    """Result of validating an image URL."""

    valid: bool
    content_type: str | None = None
    error: str | None = None


async def validate_image_url(url: str) -> ImageValidationResult:
    """Validate that a URL points to an accessible image.

    Performs a HEAD request to check if the URL is accessible and returns
    an image content type.

    Args:
        url: URL to validate

    Returns:
        ImageValidationResult with validation status and details
    """
    if not url or not url.strip():
        return ImageValidationResult(valid=False, error="URL is required")

    url = url.strip()

    # Basic URL format check
    if not url.startswith(("http://", "https://")):
        return ImageValidationResult(
            valid=False, error="URL must start with http:// or https://"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.head(url)

            if response.status_code >= 400:
                return ImageValidationResult(
                    valid=False,
                    error=f"HTTP {response.status_code}: Unable to access URL",
                )

            content_type = response.headers.get("content-type", "")
            # Check for image content types
            if not content_type.startswith("image/"):
                return ImageValidationResult(
                    valid=False,
                    content_type=content_type,
                    error=f"Not an image (content-type: {content_type})",
                )

            return ImageValidationResult(valid=True, content_type=content_type)

    except httpx.TimeoutException:
        return ImageValidationResult(valid=False, error="Request timed out")
    except httpx.RequestError as e:
        logger.warning(f"Image URL validation failed: {e}")
        return ImageValidationResult(valid=False, error=f"Request failed: {e}")


async def get_latest_image_asset(
    db: AsyncSession, player_id: int
) -> PlayerImageAsset | None:
    """Fetch the most recent image asset for a player.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        PlayerImageAsset if found, None otherwise
    """
    result = await db.execute(
        select(PlayerImageAsset)
        .where(PlayerImageAsset.player_id == player_id)  # type: ignore[arg-type]
        .order_by(PlayerImageAsset.generated_at.desc())  # type: ignore[union-attr, attr-defined]
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_image_snapshot(
    db: AsyncSession,
    player: PlayerMaster,
    style: str,
    system_prompt: str,
    system_prompt_version: str,
    image_size: str,
) -> PlayerImageSnapshot:
    """Create a new image snapshot for a single-player generation.

    Args:
        db: Async database session
        player: Player to generate image for
        style: Image style (default, vector, comic, retro)
        system_prompt: Full system prompt text
        system_prompt_version: Version identifier for prompt
        image_size: Size setting (512, 1K, 2K)

    Returns:
        Created PlayerImageSnapshot
    """
    # Build a run key for admin-triggered generation
    run_key = f"admin_single_{player.id}_{style}"

    # Find the next version for this run_key
    max_version_result = await db.execute(
        select(func.max(PlayerImageSnapshot.version)).where(
            PlayerImageSnapshot.run_key == run_key,  # type: ignore[arg-type]
            PlayerImageSnapshot.style == style,  # type: ignore[arg-type]
            PlayerImageSnapshot.cohort == CohortType.current_draft,  # type: ignore[arg-type]
        )
    )
    max_version = max_version_result.scalar()
    next_version = (max_version or 0) + 1

    snapshot = PlayerImageSnapshot(
        run_key=run_key,
        version=next_version,
        is_current=False,  # Single-player generations are not marked as current
        style=style,
        cohort=CohortType.current_draft,
        draft_year=player.draft_year,
        population_size=1,
        success_count=0,
        failure_count=0,
        image_size=image_size,
        system_prompt=system_prompt,
        system_prompt_version=system_prompt_version,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def update_snapshot_counts(
    db: AsyncSession,
    snapshot: PlayerImageSnapshot,
    success: bool,
) -> None:
    """Update snapshot success/failure counts after generation.

    Args:
        db: Async database session
        snapshot: Snapshot to update
        success: Whether the generation succeeded
    """
    if success:
        snapshot.success_count += 1
    else:
        snapshot.failure_count += 1
    await db.flush()

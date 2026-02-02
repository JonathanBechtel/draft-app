"""Admin combine data service for viewing and editing player combine data.

Provides functions to fetch and update combine data (anthro, agility, shooting).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting
from app.schemas.seasons import Season


# === Dataclasses ===


@dataclass
class CombineMetricRow:
    """Single metric with raw value."""

    metric_key: str
    display_name: str
    unit: str | None
    raw_value: float | None
    formatted_value: str | None  # e.g., "7'2.5"" or "205 lbs"


@dataclass
class CombineCategoryData:
    """All metrics for one category."""

    category: str  # "anthropometrics"
    season_code: str | None = None
    metrics: list[CombineMetricRow] = field(default_factory=list)
    has_raw_data: bool = False
    record_id: int | None = None  # ID of the combine record for updates


@dataclass
class PlayerCombineContext:
    """Combine context for a player."""

    available_seasons: list[tuple[int, str]] = field(
        default_factory=list
    )  # (season_id, code)
    selected_season_id: int | None = None
    selected_season_code: str | None = None
    anthro: CombineCategoryData | None = None
    agility: CombineCategoryData | None = None
    shooting: CombineCategoryData | None = None


# === Metric Display Configuration ===

# Anthropometrics metrics in display order
ANTHRO_METRICS = [
    ("wingspan_in", "Wingspan", "in"),
    ("standing_reach_in", "Standing Reach", "in"),
    ("height_w_shoes_in", "Height (shoes)", "in"),
    ("height_wo_shoes_in", "Height (no shoes)", "in"),
    ("weight_lb", "Weight", "lbs"),
    ("body_fat_pct", "Body Fat", "%"),
    ("hand_length_in", "Hand Length", "in"),
    ("hand_width_in", "Hand Width", "in"),
]


# === Value Formatting (matches main UI) ===


def _format_height_inches(value: float | None) -> str | None:
    """Format height in inches as feet'inches" (e.g., 6'9" or 6'9.5")."""
    if value is None:
        return None
    # Round to nearest half inch
    rounded = round(value * 2) / 2
    feet = int(rounded) // 12
    inches = rounded % 12
    if inches == int(inches):
        return f"{feet}'{int(inches)}\""
    else:
        return f"{feet}'{inches}\""


def _format_weight(value: float | None) -> str | None:
    """Format weight with lbs suffix."""
    if value is None:
        return None
    return f"{int(value)} lbs"


def _format_percentage(value: float | None) -> str | None:
    """Format percentage value."""
    if value is None:
        return None
    return f"{value:.1f}%"


def _format_inches(value: float | None) -> str | None:
    """Format inches with decimal precision."""
    if value is None:
        return None
    if value == int(value):
        return f"{int(value)} in"
    return f"{value:.1f} in"


def _format_anthro_value(field_name: str, value: float | None) -> str | None:
    """Format an anthropometric value based on field type."""
    if value is None:
        return None

    if field_name in (
        "wingspan_in",
        "standing_reach_in",
        "height_w_shoes_in",
        "height_wo_shoes_in",
    ):
        return _format_height_inches(value)
    elif field_name == "weight_lb":
        return _format_weight(value)
    elif field_name == "body_fat_pct":
        return _format_percentage(value)
    elif field_name in ("hand_length_in", "hand_width_in"):
        return _format_inches(value)
    else:
        return str(value)


# Agility metrics in display order
AGILITY_METRICS = [
    ("lane_agility_time_s", "Lane Agility", "s"),
    ("shuttle_run_s", "Shuttle Run", "s"),
    ("three_quarter_sprint_s", "3/4 Court Sprint", "s"),
    ("standing_vertical_in", "Standing Vertical", "in"),
    ("max_vertical_in", "Max Vertical", "in"),
    ("bench_press_reps", "Bench Press", "reps"),
]

# Shooting drills in display order
SHOOTING_DRILLS = [
    ("off_dribble", "Off-Dribble"),
    ("spot_up", "Spot-Up"),
    ("three_point_star", "3PT Star"),
    ("midrange_star", "Midrange Star"),
    ("three_point_side", "3PT Side"),
    ("midrange_side", "Midrange Side"),
    ("free_throw", "Free Throw"),
]


def _format_agility_value(field_name: str, value: float | int | None) -> str | None:
    """Format an agility value based on field type."""
    if value is None:
        return None

    if field_name in ("standing_vertical_in", "max_vertical_in"):
        # Format verticals as inches with decimal
        if value == int(value):
            return f"{int(value)} in"
        return f"{value:.1f} in"
    elif field_name == "bench_press_reps":
        return f"{int(value)} reps"
    elif field_name in (
        "lane_agility_time_s",
        "shuttle_run_s",
        "three_quarter_sprint_s",
    ):
        return f"{value:.2f}s"
    else:
        return str(value)


def _format_shooting_result(fgm: int | None, fga: int | None) -> str | None:
    """Format shooting result as 'X/Y (Z%)'."""
    if fgm is None or fga is None:
        return None
    if fga == 0:
        return f"{fgm}/{fga}"
    pct = (fgm / fga) * 100
    return f"{fgm}/{fga} ({pct:.1f}%)"


# === Helper Functions ===


def _clean_str(val: str | None) -> str | None:
    """Clean optional string field, returning None for empty strings."""
    if val and val.strip():
        return val.strip()
    return None


def _parse_float_field(val: str | None) -> float | None:
    """Parse a float form field. Empty or None returns None."""
    if not val or not val.strip():
        return None
    try:
        return float(val.strip())
    except ValueError:
        return None


def _parse_int_field(val: str | None) -> int | None:
    """Parse an integer form field. Empty or None returns None."""
    if not val or not val.strip():
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


# === Public API ===


async def get_available_seasons(db: AsyncSession) -> list[tuple[int, str]]:
    """Get all available seasons ordered by code descending.

    Returns:
        List of (season_id, code) tuples.
    """
    result = await db.execute(
        select(Season).order_by(Season.code.desc())  # type: ignore[union-attr,attr-defined]
    )
    seasons = result.scalars().all()
    return [(s.id, s.code) for s in seasons if s.id is not None]


async def get_or_create_season(db: AsyncSession, code: str) -> Season:
    """Get or create a season by code.

    Args:
        db: Async database session
        code: Season code like "2024-25"

    Returns:
        Season record
    """
    result = await db.execute(
        select(Season).where(Season.code == code)  # type: ignore[arg-type]
    )
    season = result.scalar_one_or_none()

    if season is None:
        # Parse year from code (e.g., "2024-25" -> start_year=2024, end_year=2025)
        parts = code.split("-")
        start_year = int(parts[0])
        end_year = int(parts[0][:2] + parts[1]) if len(parts) > 1 else start_year + 1
        season = Season(code=code, start_year=start_year, end_year=end_year)
        db.add(season)
        await db.flush()

    return season


async def _get_most_recent_anthro_season(
    db: AsyncSession,
    player_id: int,
) -> tuple[int, str] | None:
    """Find the most recent season with anthro data for a player.

    Returns:
        Tuple of (season_id, season_code) or None if no data exists.
    """
    result = await db.execute(
        select(CombineAnthro.season_id, Season.code)  # type: ignore[call-overload]
        .join(Season, CombineAnthro.season_id == Season.id)  # type: ignore[arg-type]
        .where(CombineAnthro.player_id == player_id)  # type: ignore[arg-type]
        .order_by(Season.code.desc())  # type: ignore[union-attr,attr-defined]
        .limit(1)
    )
    row = result.first()
    if row:
        return (row[0], row[1])
    return None


async def get_player_combine_context(
    db: AsyncSession,
    player_id: int,
    season_id: int | None = None,
) -> PlayerCombineContext:
    """Get combine context for a player (anthropometrics only, most recent season).

    Args:
        db: Async database session
        player_id: Player's database ID
        season_id: Optional season ID (auto-detects most recent with data if None)

    Returns:
        PlayerCombineContext with anthropometric data
    """
    # Get available seasons
    available_seasons = await get_available_seasons(db)

    context = PlayerCombineContext(
        available_seasons=available_seasons,
    )

    # Find the most recent season with actual anthro data for this player
    if season_id is None:
        recent = await _get_most_recent_anthro_season(db, player_id)
        if recent:
            season_id = recent[0]
            context.selected_season_id = season_id
            context.selected_season_code = recent[1]

    # If still no season and we have any seasons available, use the most recent
    # (allows adding new data even if no existing data)
    if season_id is None and available_seasons:
        season_id = available_seasons[0][0]
        context.selected_season_id = season_id
        context.selected_season_code = available_seasons[0][1]

    if season_id is None:
        return context

    # If we got season_id from parameter, resolve the code
    if context.selected_season_code is None:
        for sid, code in available_seasons:
            if sid == season_id:
                context.selected_season_id = season_id
                context.selected_season_code = code
                break

    # Fetch all combine data
    context.anthro = await _get_anthro_data(db, player_id, season_id)
    context.agility = await _get_agility_data(db, player_id, season_id)
    context.shooting = await _get_shooting_data(db, player_id, season_id)

    return context


async def _get_anthro_data(
    db: AsyncSession,
    player_id: int,
    season_id: int,
) -> CombineCategoryData:
    """Fetch anthropometrics data for a player/season."""
    result = await db.execute(
        select(CombineAnthro)
        .where(CombineAnthro.player_id == player_id)  # type: ignore[arg-type]
        .where(CombineAnthro.season_id == season_id)  # type: ignore[arg-type]
    )
    record = result.scalar_one_or_none()

    data = CombineCategoryData(
        category="anthropometrics",
        has_raw_data=record is not None,
        record_id=record.id if record else None,
    )

    # Build metric rows with formatted values
    for field_name, display_name, unit in ANTHRO_METRICS:
        raw_value = getattr(record, field_name, None) if record else None
        formatted_value = _format_anthro_value(field_name, raw_value)

        data.metrics.append(
            CombineMetricRow(
                metric_key=field_name,
                display_name=display_name,
                unit=unit,
                raw_value=raw_value,
                formatted_value=formatted_value,
            )
        )

    return data


async def _get_agility_data(
    db: AsyncSession,
    player_id: int,
    season_id: int,
) -> CombineCategoryData:
    """Fetch agility/athletic performance data for a player/season."""
    result = await db.execute(
        select(CombineAgility)
        .where(CombineAgility.player_id == player_id)  # type: ignore[arg-type]
        .where(CombineAgility.season_id == season_id)  # type: ignore[arg-type]
    )
    record = result.scalar_one_or_none()

    data = CombineCategoryData(
        category="agility",
        has_raw_data=record is not None,
        record_id=record.id if record else None,
    )

    # Build metric rows with formatted values
    for field_name, display_name, unit in AGILITY_METRICS:
        raw_value = getattr(record, field_name, None) if record else None
        formatted_value = _format_agility_value(field_name, raw_value)

        data.metrics.append(
            CombineMetricRow(
                metric_key=field_name,
                display_name=display_name,
                unit=unit,
                raw_value=raw_value,
                formatted_value=formatted_value,
            )
        )

    return data


@dataclass
class ShootingDrillRow:
    """Single shooting drill result."""

    drill_key: str
    display_name: str
    fgm: int | None
    fga: int | None
    formatted_value: str | None  # e.g., "4/6 (66.7%)"


async def _get_shooting_data(
    db: AsyncSession,
    player_id: int,
    season_id: int,
) -> CombineCategoryData:
    """Fetch shooting data for a player/season."""
    result = await db.execute(
        select(CombineShooting)
        .where(CombineShooting.player_id == player_id)  # type: ignore[arg-type]
        .where(CombineShooting.season_id == season_id)  # type: ignore[arg-type]
    )
    record = result.scalar_one_or_none()

    data = CombineCategoryData(
        category="shooting",
        has_raw_data=record is not None,
        record_id=record.id if record else None,
    )

    # Build drill rows - we store these as metrics but with FGM/FGA info
    for drill_key, display_name in SHOOTING_DRILLS:
        fgm = getattr(record, f"{drill_key}_fgm", None) if record else None
        fga = getattr(record, f"{drill_key}_fga", None) if record else None
        formatted_value = _format_shooting_result(fgm, fga)

        # Store FGM/FGA as raw_value is not ideal, but we need the formatted result
        # We'll use a special encoding or just store None and rely on formatted_value
        data.metrics.append(
            CombineMetricRow(
                metric_key=drill_key,
                display_name=display_name,
                unit=None,
                raw_value=None,  # Shooting has FGM/FGA pairs, not single values
                formatted_value=formatted_value,
            )
        )

    return data


# === Form Data Classes ===


@dataclass
class CombineAnthroFormData:
    """Raw form data for anthro fields."""

    wingspan_in: str | None = None
    standing_reach_in: str | None = None
    height_w_shoes_in: str | None = None
    height_wo_shoes_in: str | None = None
    weight_lb: str | None = None
    body_fat_pct: str | None = None
    hand_length_in: str | None = None
    hand_width_in: str | None = None


@dataclass
class CombineAgilityFormData:
    """Raw form data for agility fields."""

    lane_agility_time_s: str | None = None
    shuttle_run_s: str | None = None
    three_quarter_sprint_s: str | None = None
    standing_vertical_in: str | None = None
    max_vertical_in: str | None = None
    bench_press_reps: str | None = None


@dataclass
class CombineShootingFormData:
    """Raw form data for shooting fields."""

    off_dribble_fgm: str | None = None
    off_dribble_fga: str | None = None
    spot_up_fgm: str | None = None
    spot_up_fga: str | None = None
    three_point_star_fgm: str | None = None
    three_point_star_fga: str | None = None
    midrange_star_fgm: str | None = None
    midrange_star_fga: str | None = None
    three_point_side_fgm: str | None = None
    three_point_side_fga: str | None = None
    midrange_side_fgm: str | None = None
    midrange_side_fga: str | None = None
    free_throw_fgm: str | None = None
    free_throw_fga: str | None = None


# === Update Functions ===


async def update_combine_anthro(
    db: AsyncSession,
    player_id: int,
    season_id: int,
    data: CombineAnthroFormData,
) -> CombineAnthro:
    """Create or update anthropometrics data for a player/season.

    Args:
        db: Async database session
        player_id: Player's database ID
        season_id: Season ID
        data: Form data for anthro fields

    Returns:
        The created or updated CombineAnthro record
    """
    # Fetch existing or create new
    result = await db.execute(
        select(CombineAnthro)
        .where(CombineAnthro.player_id == player_id)  # type: ignore[arg-type]
        .where(CombineAnthro.season_id == season_id)  # type: ignore[arg-type]
    )
    record = result.scalar_one_or_none()

    if record is None:
        record = CombineAnthro(player_id=player_id, season_id=season_id)
        db.add(record)

    # Parse and set fields
    record.wingspan_in = _parse_float_field(data.wingspan_in)
    record.standing_reach_in = _parse_float_field(data.standing_reach_in)
    record.height_w_shoes_in = _parse_float_field(data.height_w_shoes_in)
    record.height_wo_shoes_in = _parse_float_field(data.height_wo_shoes_in)
    record.weight_lb = _parse_float_field(data.weight_lb)
    record.body_fat_pct = _parse_float_field(data.body_fat_pct)
    record.hand_length_in = _parse_float_field(data.hand_length_in)
    record.hand_width_in = _parse_float_field(data.hand_width_in)
    record.ingested_at = datetime.utcnow()

    await db.flush()
    return record


async def update_combine_agility(
    db: AsyncSession,
    player_id: int,
    season_id: int,
    data: CombineAgilityFormData,
) -> CombineAgility:
    """Create or update agility data for a player/season.

    Args:
        db: Async database session
        player_id: Player's database ID
        season_id: Season ID
        data: Form data for agility fields

    Returns:
        The created or updated CombineAgility record
    """
    result = await db.execute(
        select(CombineAgility)
        .where(CombineAgility.player_id == player_id)  # type: ignore[arg-type]
        .where(CombineAgility.season_id == season_id)  # type: ignore[arg-type]
    )
    record = result.scalar_one_or_none()

    if record is None:
        record = CombineAgility(player_id=player_id, season_id=season_id)
        db.add(record)

    record.lane_agility_time_s = _parse_float_field(data.lane_agility_time_s)
    record.shuttle_run_s = _parse_float_field(data.shuttle_run_s)
    record.three_quarter_sprint_s = _parse_float_field(data.three_quarter_sprint_s)
    record.standing_vertical_in = _parse_float_field(data.standing_vertical_in)
    record.max_vertical_in = _parse_float_field(data.max_vertical_in)
    record.bench_press_reps = _parse_int_field(data.bench_press_reps)
    record.ingested_at = datetime.utcnow()

    await db.flush()
    return record


async def update_combine_shooting(
    db: AsyncSession,
    player_id: int,
    season_id: int,
    data: CombineShootingFormData,
) -> CombineShooting:
    """Create or update shooting data for a player/season.

    Args:
        db: Async database session
        player_id: Player's database ID
        season_id: Season ID
        data: Form data for shooting fields

    Returns:
        The created or updated CombineShooting record
    """
    result = await db.execute(
        select(CombineShooting)
        .where(CombineShooting.player_id == player_id)  # type: ignore[arg-type]
        .where(CombineShooting.season_id == season_id)  # type: ignore[arg-type]
    )
    record = result.scalar_one_or_none()

    if record is None:
        record = CombineShooting(player_id=player_id, season_id=season_id)
        db.add(record)

    record.off_dribble_fgm = _parse_int_field(data.off_dribble_fgm)
    record.off_dribble_fga = _parse_int_field(data.off_dribble_fga)
    record.spot_up_fgm = _parse_int_field(data.spot_up_fgm)
    record.spot_up_fga = _parse_int_field(data.spot_up_fga)
    record.three_point_star_fgm = _parse_int_field(data.three_point_star_fgm)
    record.three_point_star_fga = _parse_int_field(data.three_point_star_fga)
    record.midrange_star_fgm = _parse_int_field(data.midrange_star_fgm)
    record.midrange_star_fga = _parse_int_field(data.midrange_star_fga)
    record.three_point_side_fgm = _parse_int_field(data.three_point_side_fgm)
    record.three_point_side_fga = _parse_int_field(data.three_point_side_fga)
    record.midrange_side_fgm = _parse_int_field(data.midrange_side_fgm)
    record.midrange_side_fga = _parse_int_field(data.midrange_side_fga)
    record.free_throw_fgm = _parse_int_field(data.free_throw_fgm)
    record.free_throw_fga = _parse_int_field(data.free_throw_fga)
    record.ingested_at = datetime.utcnow()

    await db.flush()
    return record

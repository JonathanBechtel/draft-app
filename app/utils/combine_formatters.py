"""Shared formatting helpers for combine measurement values.

Used by both admin combine service and public stats service.
"""

from __future__ import annotations


def format_height_inches(value: float | None) -> str | None:
    """Format height in inches as feet'inches" (e.g., 6'9" or 6'9.5")."""
    if value is None:
        return None
    rounded = round(value * 4) / 4
    feet = int(rounded) // 12
    inches = rounded % 12
    if inches == int(inches):
        return f"{feet}'{int(inches)}\""
    return f"{feet}'{inches}\""


def format_weight(value: float | None) -> str | None:
    """Format weight with lbs suffix."""
    if value is None:
        return None
    return f"{int(value)} lbs"


def format_percentage(value: float | None) -> str | None:
    """Format percentage value."""
    if value is None:
        return None
    return f"{value:.1f}%"


def format_inches(value: float | None) -> str | None:
    """Format inches with decimal precision."""
    if value is None:
        return None
    if value == int(value):
        return f"{int(value)} in"
    return f"{value:.2f} in"


def format_anthro_value(field_name: str, value: float | None) -> str | None:
    """Format an anthropometric value based on field type."""
    if value is None:
        return None

    if field_name in (
        "wingspan_in",
        "standing_reach_in",
        "height_w_shoes_in",
        "height_wo_shoes_in",
    ):
        return format_height_inches(value)
    elif field_name == "weight_lb":
        return format_weight(value)
    elif field_name == "body_fat_pct":
        return format_percentage(value)
    elif field_name in ("hand_length_in", "hand_width_in"):
        return format_inches(value)
    else:
        return str(value)


def format_agility_value(field_name: str, value: float | int | None) -> str | None:
    """Format an agility value based on field type."""
    if value is None:
        return None

    if field_name in ("standing_vertical_in", "max_vertical_in"):
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


def format_shooting_result(fgm: int | None, fga: int | None) -> str | None:
    """Format shooting result as 'X/Y (Z%)'."""
    if fgm is None or fga is None:
        return None
    if fga == 0:
        return f"{fgm}/{fga}"
    pct = (fgm / fga) * 100
    return f"{fgm}/{fga} ({pct:.1f}%)"

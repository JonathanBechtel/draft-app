from typing import Dict, Optional, Tuple
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


# Drill keys are shared between ingestion and metric computation so we define the
# column mapping once here.
SHOOTING_DRILL_COLUMNS: Dict[str, Tuple[str, str]] = {
    "off_dribble": ("off_dribble_fgm", "off_dribble_fga"),
    "spot_up": ("spot_up_fgm", "spot_up_fga"),
    "three_point_star": ("three_point_star_fgm", "three_point_star_fga"),
    "midrange_star": ("midrange_star_fgm", "midrange_star_fga"),
    "three_point_side": ("three_point_side_fgm", "three_point_side_fga"),
    "midrange_side": ("midrange_side_fgm", "midrange_side_fga"),
    "free_throw": ("free_throw_fgm", "free_throw_fga"),
}

# Maps drill key → pct column name on CombineShooting
SHOOTING_PCT_COLUMNS: Dict[str, str] = {
    "off_dribble": "off_dribble_pct",
    "spot_up": "spot_up_pct",
    "three_point_star": "three_point_star_pct",
    "midrange_star": "midrange_star_pct",
    "three_point_side": "three_point_side_pct",
    "midrange_side": "midrange_side_pct",
    "free_throw": "free_throw_pct",
}


def compute_shooting_pct(fgm: int | None, fga: int | None) -> float | None:
    """Compute shooting percentage from makes and attempts.

    Returns percentage (0-100) or None if attempts are missing/zero.
    """
    if fgm is None or fga is None or fga == 0:
        return None
    return round((fgm / fga) * 100, 1)


class CombineShooting(SQLModel, table=True):  # type: ignore[call-arg]
    """Shooting results per player-season (wide format, one row per combine entry)."""

    __tablename__ = "combine_shooting_results"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_shooting_player_season"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season_id: int = Field(foreign_key="seasons.id", index=True)
    position_id: Optional[int] = Field(
        default=None, foreign_key="positions.id", index=True
    )

    raw_position: Optional[str] = Field(default=None, index=True)

    off_dribble_fgm: Optional[int] = Field(default=None)
    off_dribble_fga: Optional[int] = Field(default=None)
    spot_up_fgm: Optional[int] = Field(default=None)
    spot_up_fga: Optional[int] = Field(default=None)
    three_point_star_fgm: Optional[int] = Field(default=None)
    three_point_star_fga: Optional[int] = Field(default=None)
    midrange_star_fgm: Optional[int] = Field(default=None)
    midrange_star_fga: Optional[int] = Field(default=None)
    three_point_side_fgm: Optional[int] = Field(default=None)
    three_point_side_fga: Optional[int] = Field(default=None)
    midrange_side_fgm: Optional[int] = Field(default=None)
    midrange_side_fga: Optional[int] = Field(default=None)
    free_throw_fgm: Optional[int] = Field(default=None)
    free_throw_fga: Optional[int] = Field(default=None)

    # Pre-computed shooting percentages (fgm / fga * 100), NULL when fga is 0 or NULL
    off_dribble_pct: Optional[float] = Field(default=None)
    spot_up_pct: Optional[float] = Field(default=None)
    three_point_star_pct: Optional[float] = Field(default=None)
    midrange_star_pct: Optional[float] = Field(default=None)
    three_point_side_pct: Optional[float] = Field(default=None)
    midrange_side_pct: Optional[float] = Field(default=None)
    free_throw_pct: Optional[float] = Field(default=None)

    nba_stats_player_id: Optional[int] = Field(default=None)
    raw_player_name: Optional[str] = Field(default=None)
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

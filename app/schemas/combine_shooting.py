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

    nba_stats_player_id: Optional[int] = Field(default=None)
    raw_player_name: Optional[str] = Field(default=None)
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

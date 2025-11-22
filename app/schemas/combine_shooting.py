from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class CombineShootingResult(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "combine_shooting_results"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season_id", "drill", name="uq_shooting_player_season_drill"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season_id: int = Field(foreign_key="seasons.id", index=True)
    position_id: Optional[int] = Field(
        default=None, foreign_key="positions.id", index=True
    )

    raw_position: Optional[str] = Field(default=None, index=True)
    drill: str = Field(
        index=True,
        description="off_dribble | spot_up | three_point_star | midrange_star | three_point_side | midrange_side | free_throw",
    )

    fgm: Optional[int] = Field(default=None)
    fga: Optional[int] = Field(default=None)

    nba_stats_player_id: Optional[int] = Field(default=None)
    raw_player_name: Optional[str] = Field(default=None)
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

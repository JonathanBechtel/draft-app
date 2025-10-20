from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class CombineAgility(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "combine_agility"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_agility_player_season"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season_id: int = Field(foreign_key="seasons.id", index=True)

    pos: Optional[str] = Field(default=None, index=True)

    lane_agility_time_s: Optional[float] = Field(default=None)
    shuttle_run_s: Optional[float] = Field(default=None)
    three_quarter_sprint_s: Optional[float] = Field(default=None)
    standing_vertical_in: Optional[float] = Field(default=None)
    max_vertical_in: Optional[float] = Field(default=None)
    bench_press_reps: Optional[int] = Field(default=None)

    nba_stats_player_id: Optional[int] = Field(default=None)
    raw_player_name: Optional[str] = Field(default=None)
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

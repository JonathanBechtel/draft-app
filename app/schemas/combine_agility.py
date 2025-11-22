from typing import List, Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint, Column
from sqlalchemy.dialects.postgresql import JSONB


class CombineAgility(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "combine_agility"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_agility_player_season"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season_id: int = Field(foreign_key="seasons.id", index=True)
    position_id: Optional[int] = Field(
        default=None, foreign_key="positions.id", index=True
    )

    raw_position: Optional[str] = Field(default=None, index=True)
    position_fine: Optional[str] = Field(default=None, index=True)
    position_parents: Optional[List[str]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    lane_agility_time_s: Optional[float] = Field(default=None)
    shuttle_run_s: Optional[float] = Field(default=None)
    three_quarter_sprint_s: Optional[float] = Field(default=None)
    standing_vertical_in: Optional[float] = Field(default=None)
    max_vertical_in: Optional[float] = Field(default=None)
    bench_press_reps: Optional[int] = Field(default=None)

    nba_stats_player_id: Optional[int] = Field(default=None)
    raw_player_name: Optional[str] = Field(default=None)
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

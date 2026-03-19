from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class PlayerCollegeStats(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_college_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "season", name="uq_college_stats_player_season"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season: str = Field(index=True, description="Season label, e.g. '2024-25'")

    # Per-game averages
    games: Optional[int] = Field(default=None)
    games_started: Optional[int] = Field(default=None)
    mpg: Optional[float] = Field(default=None)
    ppg: Optional[float] = Field(default=None)
    rpg: Optional[float] = Field(default=None)
    apg: Optional[float] = Field(default=None)
    spg: Optional[float] = Field(default=None)
    bpg: Optional[float] = Field(default=None)
    tov: Optional[float] = Field(default=None)
    pf: Optional[float] = Field(default=None)

    # Shooting
    fg_pct: Optional[float] = Field(default=None)
    three_p_pct: Optional[float] = Field(default=None)
    three_pa: Optional[float] = Field(default=None)
    ft_pct: Optional[float] = Field(default=None)
    fta: Optional[float] = Field(default=None)

    # Provenance
    source: Optional[str] = Field(
        default=None, description="Data source, e.g. 'ai_generated', 'sports_reference'"
    )
    updated_at: datetime = Field(default_factory=datetime.utcnow)

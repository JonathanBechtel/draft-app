from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class PlayerStatus(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_status"
    __table_args__ = (UniqueConstraint("player_id", name="uq_player_status_player"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)

    # Ephemeral state
    is_active_nba: Optional[bool] = Field(default=None, index=True)
    current_team: Optional[str] = Field(default=None, index=True)
    nba_last_season: Optional[str] = Field(default=None, index=True)

    # Listed attributes that may change
    position: Optional[str] = Field(default=None)
    height_in: Optional[int] = Field(default=None)
    weight_lb: Optional[int] = Field(default=None)

    source: Optional[str] = Field(
        default=None, description="Provenance key, e.g., 'bbr'"
    )
    updated_at: datetime = Field(default_factory=datetime.utcnow)

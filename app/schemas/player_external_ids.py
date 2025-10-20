from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class PlayerExternalId(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_external_ids"
    __table_args__ = (
        UniqueConstraint("system", "external_id", name="uq_external_system_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)

    system: str = Field(
        description="External system key, e.g., 'nba_stats', 'bbr', 'espn'"
    )
    external_id: str = Field(description="External identifier string")
    source_url: Optional[str] = Field(default=None)

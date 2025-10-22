from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class PlayerAlias(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_aliases"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "full_name", name="uq_player_aliases_player_fullname"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)

    full_name: str = Field(index=True, description="Alias as a single string")

    prefix: Optional[str] = Field(default=None)
    first_name: Optional[str] = Field(default=None)
    middle_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None)
    suffix: Optional[str] = Field(default=None)

    context: Optional[str] = Field(
        default=None, description="Provenance note or system source"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)

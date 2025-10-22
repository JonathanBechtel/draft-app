from typing import Optional
from datetime import datetime, date
from sqlmodel import SQLModel, Field


class PlayerMaster(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "players_master"

    id: Optional[int] = Field(default=None, primary_key=True)

    prefix: Optional[str] = Field(default=None)
    first_name: Optional[str] = Field(default=None, index=True)
    middle_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None, index=True)
    suffix: Optional[str] = Field(default=None)

    display_name: Optional[str] = Field(default=None, index=True)
    birthdate: Optional[date] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

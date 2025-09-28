from sqlmodel import SQLModel, Field as SQLField
from sqlalchemy import Column
from sqlalchemy import Enum as SAEnum
from datetime import date
from pydantic import (computed_field, 
                        field_validator)

from app.models.fields import Position, BIRTH_DATE


class PlayerBase(SQLModel):
    name: str
    # Keep Pydantic field name "position", map DB column to "player_position"
    position: Position = SQLField(
        sa_column=Column(
            "player_position",
            SAEnum(Position, name="player_position_enum"),
            nullable=False,
        )
    )
    school: str
    birth_date: BIRTH_DATE

    @field_validator("birth_date")
    @classmethod
    def not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("birth_date cannot be in the future")
        return v

class PlayerRead(PlayerBase):
    id: int
        
    @computed_field
    @property
    def age(self) -> float:
        days = (date.today() - self.birth_date).days
        return round(days / 365.2425, 2)

class PlayerCreate(PlayerBase):
    pass


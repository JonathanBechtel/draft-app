from sqlmodel import SQLModel, Field
from typing import Literal, Annotated
from datetime import date

BIRTH_YEAR = Annotated[date, Field(..., ge=date(1980, 1, 1))]

class Player(SQLModel, table=True):
    # do you need to specify autoincrement?
    id: int = Field(default=None, primary_key=True)
    name: str
    position: str = Literal["forward", "center", "guard"]
    school: str
    age: int = Field(default=18, ge=18)
    birth_year = BIRTH_YEAR






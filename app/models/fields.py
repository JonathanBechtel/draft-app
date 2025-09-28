"""
Contains PyDantic fields to be used in various models.
"""
from enum import Enum
from datetime import date
from typing import Annotated
from pydantic import Field as PydField

class Position(str, Enum):
    g = "g"
    f = "f"
    c = "c"

    @property
    def label(self) -> str:
        return {
            "g": "guard",
            "f": "forward",
            "c": "center",
        }[self.value]

BIRTH_DATE = Annotated[date, PydField(..., ge=date(1980, 1, 1))]

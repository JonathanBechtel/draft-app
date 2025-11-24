from typing import Optional, List
from sqlmodel import SQLModel, Field
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB


class Position(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "positions"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(
        unique=True, index=True, description="Short code like PG, C, PG-SG"
    )
    description: Optional[str] = Field(default=None)
    parents: Optional[List[str]] = Field(default=None, sa_column=Column(JSONB))

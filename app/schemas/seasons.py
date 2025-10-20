from typing import Optional
from sqlmodel import SQLModel, Field


class Season(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "seasons"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True, description="Season code like '2024-25'")
    start_year: int
    end_year: int
    # Optional dates kept out for now to minimize scope

from typing import Optional
from datetime import datetime

from sqlmodel import SQLModel, Field


class CollegeSchool(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "college_schools"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(
        unique=True, index=True, description="Official school name, e.g. 'Duke'"
    )
    slug: str = Field(
        unique=True, index=True, description="URL-safe identifier, e.g. 'duke'"
    )
    conference: Optional[str] = Field(
        default=None, index=True, description="e.g. 'ACC'"
    )
    espn_id: Optional[int] = Field(
        default=None, description="ESPN team ID for logo retrieval"
    )

    logo_url: Optional[str] = Field(
        default=None, description="S3/CDN path to school logo"
    )
    primary_color: Optional[str] = Field(
        default=None, description="Hex color, e.g. '#003087'"
    )
    secondary_color: Optional[str] = Field(
        default=None, description="Hex color, e.g. '#FFFFFF'"
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

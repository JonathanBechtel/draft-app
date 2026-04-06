from typing import Optional
from datetime import datetime

from sqlmodel import SQLModel, Field


class NbaTeam(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "nba_teams"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(
        index=True, description="Full team name, e.g. 'Los Angeles Lakers'"
    )
    abbreviation: str = Field(
        unique=True, index=True, description="Standard 3-letter code, e.g. 'LAL'"
    )
    slug: str = Field(
        unique=True, index=True, description="URL-safe identifier, e.g. 'lakers'"
    )
    city: Optional[str] = Field(
        default=None, description="Team city, e.g. 'Los Angeles'"
    )
    conference: Optional[str] = Field(
        default=None, description="'Eastern' or 'Western'"
    )
    division: Optional[str] = Field(default=None)

    logo_url: Optional[str] = Field(
        default=None, description="S3/CDN path to team logo"
    )
    primary_color: Optional[str] = Field(
        default=None, description="Hex color, e.g. '#552583'"
    )
    secondary_color: Optional[str] = Field(
        default=None, description="Hex color, e.g. '#FDB927'"
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

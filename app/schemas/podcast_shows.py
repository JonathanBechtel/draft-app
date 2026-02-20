"""Podcast show configuration table."""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class PodcastShow(SQLModel, table=True):  # type: ignore[call-arg]
    """Configuration for a podcast show.

    Admin-curated: find a good podcast, paste its RSS feed URL,
    and episodes are fetched on a schedule.
    """

    __tablename__ = "podcast_shows"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    display_name: str
    feed_url: str = Field(unique=True)
    artwork_url: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    website_url: Optional[str] = Field(default=None)
    is_draft_focused: bool = Field(
        default=True,
        description="When True, all episodes ingested without relevance checks",
    )
    is_active: bool = Field(default=True, index=True)
    fetch_interval_minutes: int = Field(default=30)
    last_fetched_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

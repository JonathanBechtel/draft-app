"""YouTube channel configuration table for film-room ingestion."""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class YouTubeChannel(SQLModel, table=True):  # type: ignore[call-arg]
    """Admin-curated YouTube source channel."""

    __tablename__ = "youtube_channels"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    display_name: str
    channel_id: str = Field(unique=True)
    channel_url: Optional[str] = Field(default=None)
    thumbnail_url: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    uploads_playlist_id: Optional[str] = Field(default=None)
    is_draft_focused: bool = Field(default=True)
    is_active: bool = Field(default=True, index=True)
    fetch_interval_minutes: int = Field(default=60)
    last_fetched_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

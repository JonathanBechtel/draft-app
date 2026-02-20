"""Pydantic request/response models for the podcast feature."""

from typing import Optional

from sqlmodel import SQLModel


class PodcastEpisodeRead(SQLModel):
    """Response model for a podcast episode in the feed."""

    id: int
    show_name: str
    artwork_url: Optional[str] = None
    title: str
    summary: str
    tag: str  # PodcastEpisodeTag display value
    audio_url: str
    episode_url: Optional[str] = None
    duration: str  # Formatted "45:23" or "1:02:03"
    time: str  # Relative time "2h", "1d"
    listen_on_text: str  # "Listen on The Ringer"
    is_player_specific: bool = False


class PodcastFeedResponse(SQLModel):
    """Response model for paginated podcast feed."""

    items: list[PodcastEpisodeRead]
    total: int
    limit: int
    offset: int


class PodcastShowRead(SQLModel):
    """Response model for a podcast show (admin view)."""

    id: int
    name: str
    display_name: str
    feed_url: str
    artwork_url: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    website_url: Optional[str] = None
    is_draft_focused: bool
    is_active: bool
    fetch_interval_minutes: int
    last_fetched_at: Optional[str] = None  # ISO format string


class PodcastShowCreate(SQLModel):
    """Request model for creating a podcast show."""

    name: str
    display_name: str
    feed_url: str
    artwork_url: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    website_url: Optional[str] = None
    is_draft_focused: bool = True
    fetch_interval_minutes: int = 30


class PodcastIngestionResult(SQLModel):
    """Response model for podcast ingestion cycle results."""

    shows_processed: int
    episodes_added: int
    episodes_skipped: int
    episodes_filtered: int = 0  # Failed relevance check
    mentions_added: int = 0
    errors: list[str]

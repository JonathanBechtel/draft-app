"""Pydantic request/response models for film-room videos."""

from typing import Optional

from sqlmodel import SQLModel

from app.models.content_mentions import MentionedPlayer


class YouTubeVideoRead(SQLModel):
    """Response model for a single film-room video card."""

    id: int
    channel_name: str
    thumbnail_url: Optional[str] = None
    title: str
    summary: str
    tag: str
    youtube_url: str
    youtube_embed_id: str
    duration: str
    time: str
    view_count_display: str
    watch_on_text: str
    is_player_specific: bool = False
    mentioned_players: list[MentionedPlayer] = []


class VideoFeedResponse(SQLModel):
    """Response model for paginated video feed."""

    items: list[YouTubeVideoRead]
    total: int
    limit: int
    offset: int


class YouTubeChannelRead(SQLModel):
    """Response model for a YouTube source channel."""

    id: int
    name: str
    display_name: str
    channel_id: str
    channel_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None
    is_draft_focused: bool
    is_active: bool
    fetch_interval_minutes: int
    last_fetched_at: Optional[str] = None


class YouTubeChannelCreate(SQLModel):
    """Request model for creating a YouTube source channel."""

    name: str
    display_name: str
    channel_id: str
    channel_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None
    is_draft_focused: bool = True
    fetch_interval_minutes: int = 60


class YouTubeChannelUpdate(SQLModel):
    """Request model for updating a YouTube source channel."""

    name: str
    display_name: str
    channel_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    description: Optional[str] = None
    is_draft_focused: bool = True
    is_active: bool = True
    fetch_interval_minutes: int = 60


class VideoIngestionResult(SQLModel):
    """Response model for ingestion cycle results."""

    channels_processed: int
    videos_added: int
    videos_skipped: int
    videos_filtered: int = 0
    mentions_added: int = 0
    errors: list[str]


class ManualVideoAddRequest(SQLModel):
    """Request model for manual URL-based video add."""

    youtube_url: str
    tag: Optional[str] = None
    player_ids: list[int] = []


class ManualVideoUpdateRequest(SQLModel):
    """Request model for manual video metadata update."""

    title: Optional[str] = None
    summary: Optional[str] = None
    tag: Optional[str] = None
    player_ids: list[int] = []

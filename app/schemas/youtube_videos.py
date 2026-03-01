"""YouTube video table for film-room content."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, Column, Enum as SAEnum, Index
from sqlmodel import Field, SQLModel


class YouTubeVideoTag(str, Enum):
    """Classification tags for YouTube videos."""

    THINK_PIECE = "Think Piece"
    CONVERSATION = "Conversation"
    SCOUTING_REPORT = "Scouting Report"
    HIGHLIGHTS = "Highlights"
    MONTAGE = "Montage"


class YouTubeVideo(SQLModel, table=True):  # type: ignore[call-arg]
    """A curated or ingested YouTube video."""

    __tablename__ = "youtube_videos"
    __table_args__ = (
        Index("ix_youtube_videos_published_at", "published_at"),
        Index("ix_youtube_videos_channel_published", "channel_id", "published_at"),
        Index("ix_youtube_videos_tag_published", "tag", "published_at"),
        Index("ix_youtube_videos_external_id", "external_id", unique=True),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0"),
        CheckConstraint("view_count IS NULL OR view_count >= 0"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="youtube_channels.id")
    external_id: str
    title: str
    description: Optional[str] = Field(default=None)
    youtube_url: str
    thumbnail_url: Optional[str] = Field(default=None)
    duration_seconds: Optional[int] = Field(default=None)
    view_count: Optional[int] = Field(default=None)
    summary: Optional[str] = Field(default=None)
    tag: YouTubeVideoTag = Field(
        default=YouTubeVideoTag.SCOUTING_REPORT,
        sa_column=Column(
            SAEnum(YouTubeVideoTag, name="youtubevideotag"),
            nullable=False,
            server_default="SCOUTING_REPORT",
        ),
    )
    published_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_manually_added: bool = Field(default=False)

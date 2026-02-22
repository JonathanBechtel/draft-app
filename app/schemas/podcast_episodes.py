"""Podcast episodes table for storing ingested episode content."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class PodcastEpisodeTag(str, Enum):
    """Classification tags for podcast episodes."""

    INTERVIEW = "Interview"
    DRAFT_ANALYSIS = "Draft Analysis"
    MOCK_DRAFT = "Mock Draft"
    GAME_BREAKDOWN = "Game Breakdown"
    TRADE_INTEL = "Trade & Intel"
    PROSPECT_DEBATE = "Prospect Debate"
    MAILBAG = "Mailbag"
    EVENT_PREVIEW = "Event Preview"


class PodcastEpisode(SQLModel, table=True):  # type: ignore[call-arg]
    """A podcast episode from an ingested show feed.

    Stores both original content from the RSS feed and AI-generated fields
    (summary, tag classification).
    """

    __tablename__ = "podcast_episodes"
    __table_args__ = (
        UniqueConstraint(
            "show_id", "external_id", name="uq_podcast_episodes_show_external"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    show_id: int = Field(foreign_key="podcast_shows.id", index=True)
    external_id: str = Field(index=True)  # RSS guid for deduplication

    # Original content from feed
    title: str
    description: Optional[str] = Field(default=None)
    audio_url: str
    duration_seconds: Optional[int] = Field(default=None)
    episode_url: Optional[str] = Field(default=None)
    artwork_url: Optional[str] = Field(default=None)
    season: Optional[int] = Field(default=None)
    episode_number: Optional[int] = Field(default=None)

    # AI-generated fields
    summary: Optional[str] = Field(default=None)

    # Classification
    tag: PodcastEpisodeTag = Field(default=PodcastEpisodeTag.DRAFT_ANALYSIS)

    # Timestamps
    published_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Player association
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")

"""Polymorphic junction table for player mentions across content types."""

from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Index, String, UniqueConstraint
from sqlmodel import Field, SQLModel


class ContentType(str, Enum):
    """Type of content a player is mentioned in."""

    NEWS = "news"
    PODCAST = "podcast"


class MentionSource(str, Enum):
    """How a player mention was detected."""

    AI = "ai"
    BACKFILL = "backfill"
    MANUAL = "manual"


class PlayerContentMention(SQLModel, table=True):  # type: ignore[call-arg]
    """Tracks which players are mentioned in which content items."""

    __tablename__ = "player_content_mentions"
    __table_args__ = (
        UniqueConstraint(
            "content_type",
            "content_id",
            "player_id",
            name="uq_content_mention",
        ),
        Index(
            "ix_pcm_player_created",
            "player_id",
            "created_at",
        ),
        Index(
            "ix_pcm_content_lookup",
            "content_type",
            "content_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    content_type: str = Field(
        sa_column=Column("content_type", String, nullable=False),
    )
    content_id: int = Field(description="ID of the content item (polymorphic, no FK)")
    published_at: Optional[datetime] = Field(
        default=None, description="Denormalized from content table"
    )
    source: str = Field(
        sa_column=Column("source", String, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC).replace(tzinfo=None)
    )

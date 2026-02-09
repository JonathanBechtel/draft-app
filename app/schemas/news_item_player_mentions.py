"""Junction table for news item ↔ player mention associations."""

from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class MentionSource(str, Enum):
    """How a player mention was detected."""

    AI = "ai"
    BACKFILL = "backfill"
    MANUAL = "manual"


class NewsItemPlayerMention(SQLModel, table=True):  # type: ignore[call-arg]
    """Tracks which players are mentioned in which news articles."""

    __tablename__ = "news_item_player_mentions"
    __table_args__ = (
        UniqueConstraint(
            "news_item_id",
            "player_id",
            name="uq_news_item_player_mention",
        ),
        Index(
            "ix_mentions_player_created",
            "player_id",
            "created_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    news_item_id: int = Field(foreign_key="news_items.id", index=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    source: MentionSource = Field(description="Detection method")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC).replace(tzinfo=None)
    )

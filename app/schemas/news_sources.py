"""News source configuration table (feed-type-agnostic)."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class FeedType(str, Enum):
    """Supported feed types for news sources."""

    RSS = "rss"
    # Future: API = "api", SCRAPER = "scraper", etc.


class NewsSource(SQLModel, table=True):  # type: ignore[call-arg]
    """Configuration for a news feed source.

    Designed to be feed-type-agnostic: RSS-specific logic lives in the
    ingestion service, not here.
    """

    __tablename__ = "news_sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # e.g., "Floor and Ceiling"
    display_name: str  # e.g., "Floor and Ceiling"
    feed_type: FeedType = Field(default=FeedType.RSS)
    feed_url: str = Field(unique=True)  # RSS URL or API endpoint
    is_active: bool = Field(default=True, index=True)
    fetch_interval_minutes: int = Field(default=30)
    last_fetched_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

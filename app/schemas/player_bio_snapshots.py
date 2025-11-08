from typing import Optional
from datetime import datetime
from sqlmodel import SQLModel, Field


class PlayerBioSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_bio_snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)

    source: str = Field(default="bbr", description="Source system key")
    source_url: Optional[str] = Field(default=None)
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    raw_meta_html: Optional[str] = Field(
        default=None, description="Raw HTML snippet of meta box"
    )

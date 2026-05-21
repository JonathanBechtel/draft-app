"""X (Twitter) post history for the autonomous thread-drafting skill."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import Column, Enum as SAEnum, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class XPostAngle(str, Enum):
    """Thread angle categories used to pick subject + drive dedup."""

    spotlight = "spotlight"
    h2h = "h2h"
    outlier = "outlier"
    consensus_shift = "consensus_shift"
    news_tag = "news_tag"


class XPostStatus(str, Enum):
    """Lifecycle state of a drafted post."""

    draft = "draft"
    posted = "posted"
    skipped = "skipped"


class XPostHistory(SQLModel, table=True):  # type: ignore[call-arg]
    """One row per generated X thread, kept indefinitely for dedup and audit."""

    __tablename__ = "x_post_history"
    __table_args__ = (
        Index("ix_x_post_history_angle_created", "angle", "created_at"),
        Index("ix_x_post_history_status_created", "status", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    angle: XPostAngle = Field(
        sa_column=Column(
            SAEnum(XPostAngle, name="x_post_angle_enum"),
            nullable=False,
        )
    )
    status: XPostStatus = Field(
        default=XPostStatus.draft,
        sa_column=Column(
            SAEnum(XPostStatus, name="x_post_status_enum"),
            nullable=False,
            server_default=XPostStatus.draft.value,
        ),
    )

    # Subject(s) — either players or a news item. Both stored as arrays so a
    # single angle (e.g. h2h) can reference multiple players cleanly.
    player_ids: list[int] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        description="Players referenced by this thread (anchor first for h2h).",
    )
    news_item_id: Optional[int] = Field(
        default=None,
        foreign_key="news_items.id",
        description="News item referenced by news_tag angle, if any.",
    )

    # The thread itself. tweets is an ordered list of {text, image_path} dicts.
    headline: Optional[str] = Field(
        default=None,
        description="Short human-readable label, e.g. 'Outlier: Cooper Flagg wingspan'.",
    )
    tweets: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        description="Ordered tweets in the thread. Each dict has at minimum 'text'.",
    )
    image_paths: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        description="Local relative paths to PNGs attached to the lead tweet.",
    )

    draft_dir: Optional[str] = Field(
        default=None,
        description="Relative path under scripts/x_threads/drafts/ where files live.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-form analyst notes about why this angle was picked.",
    )

    # Posting metadata (populated when status flips to POSTED).
    posted_at: Optional[datetime] = Field(default=None)
    external_post_id: Optional[str] = Field(
        default=None,
        description="X tweet ID of the lead tweet once posted.",
    )

    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

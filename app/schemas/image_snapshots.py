"""Image generation snapshot and asset tables for audit trail."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from app.models.fields import CohortType


class BatchJobState(str, enum.Enum):
    """Gemini batch job states."""

    pending = "JOB_STATE_PENDING"
    running = "JOB_STATE_RUNNING"
    succeeded = "JOB_STATE_SUCCEEDED"
    failed = "JOB_STATE_FAILED"
    cancelled = "JOB_STATE_CANCELLED"
    expired = "JOB_STATE_EXPIRED"


class PlayerImageSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    """Audit trail for player image generation runs.

    Each snapshot represents a batch generation run with specific settings.
    Multiple snapshots can exist for the same cohort/style, with version tracking.
    """

    __tablename__ = "player_image_snapshots"
    __table_args__ = (
        # Unique version within a (style, cohort, run_key) group
        UniqueConstraint(
            "style",
            "cohort",
            "run_key",
            "version",
            name="uq_image_snapshots_style_cohort_run_ver",
        ),
        # Partial unique index to enforce one current per (style, cohort, draft_year)
        Index(
            "uq_image_snapshots_current",
            "style",
            "cohort",
            text("coalesce(draft_year, -1)"),
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    run_key: str = Field(
        index=True,
        description="Identifier for the batch run, e.g., 'draft_2025_v1'",
    )
    version: int = Field(
        description="Monotonic version within (style, cohort, run_key)"
    )
    is_current: bool = Field(
        default=False,
        description="Marks the active snapshot for this style/cohort context",
    )

    # Categorization
    style: str = Field(
        index=True,
        description="Image style: 'default', 'vector', 'comic', 'retro'",
    )
    cohort: CohortType = Field(
        sa_column=Column(
            SAEnum(CohortType, name="cohort_type_enum", create_type=False),
            nullable=False,
        )
    )
    draft_year: Optional[int] = Field(
        default=None,
        index=True,
        description="For draft-year-specific batches",
    )

    # Auditing
    population_size: int = Field(
        default=0,
        description="Number of players targeted in this run",
    )
    success_count: int = Field(
        default=0,
        description="Number of images successfully generated",
    )
    failure_count: int = Field(
        default=0,
        description="Number of generation failures",
    )
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    notes: Optional[str] = Field(
        default=None,
        description="Optional commentary about the run",
    )

    # Cost tracking
    image_size: str = Field(
        description="Image size setting: '512', '1K', '2K'",
    )
    estimated_cost_usd: Optional[float] = Field(
        default=None,
        description="Estimated API cost for this run",
    )

    # Prompt versioning (stored at snapshot level for batch runs)
    system_prompt: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Full system prompt used for this run",
    )
    system_prompt_version: Optional[str] = Field(
        default=None,
        description="Version identifier for prompt iteration tracking, e.g., 'v2.1'",
    )


class PlayerImageAsset(SQLModel, table=True):  # type: ignore[call-arg]
    """Individual image records linked to a snapshot.

    Stores the S3 location and full prompt details for each generated image.
    Supports future admin UI for reviewing/regenerating images.
    """

    __tablename__ = "player_image_assets"
    __table_args__ = (
        # One asset per player per snapshot
        UniqueConstraint(
            "snapshot_id",
            "player_id",
            name="uq_image_asset_snapshot_player",
        ),
        Index(
            "ix_image_assets_player",
            "player_id",
        ),
        Index(
            "ix_image_assets_snapshot",
            "snapshot_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(
        sa_column=Column(ForeignKey("player_image_snapshots.id", ondelete="CASCADE"))
    )
    player_id: int = Field(foreign_key="players_master.id")

    # S3 storage
    s3_key: str = Field(
        description="S3 object key, e.g., 'players/1661_cooper-flagg_default.png'",
    )
    s3_bucket: Optional[str] = Field(
        default=None,
        description="Bucket name (for multi-bucket support)",
    )
    public_url: str = Field(
        description="Full CDN/S3 URL for serving the image",
    )
    file_size_bytes: Optional[int] = Field(
        default=None,
        description="Image file size in bytes",
    )

    # Timing
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    generation_time_sec: Optional[float] = Field(
        default=None,
        description="Time taken to generate this image",
    )

    # Generation metadata - full prompt storage for admin UI
    user_prompt: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Full player-specific prompt sent to the API",
    )
    likeness_description: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Description generated from reference image (if used)",
    )
    used_likeness_ref: bool = Field(
        default=False,
        description="Whether a reference image was used for likeness",
    )
    reference_image_url: Optional[str] = Field(
        default=None,
        description="URL of reference image used (if any)",
    )
    error_message: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Error message if generation failed",
    )


class ImageBatchJob(SQLModel, table=True):  # type: ignore[call-arg]
    """Tracks Gemini batch jobs for async image generation.

    Stores the batch job ID and metadata needed to retrieve results
    and create PlayerImageAsset records when the job completes.
    """

    __tablename__ = "image_batch_jobs"
    __table_args__ = (
        Index("ix_batch_jobs_state", "state"),
        Index("ix_batch_jobs_snapshot", "snapshot_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Gemini batch job reference
    gemini_job_name: str = Field(
        unique=True,
        index=True,
        description="Gemini batch job name, e.g., 'batches/abc123'",
    )
    state: BatchJobState = Field(
        sa_column=Column(
            SAEnum(BatchJobState, name="batch_job_state_enum", create_type=False),
            nullable=False,
        ),
        description="Current state of the batch job",
    )

    # Link to snapshot (created at submit time)
    snapshot_id: int = Field(
        sa_column=Column(ForeignKey("player_image_snapshots.id", ondelete="CASCADE"))
    )

    # Player IDs included in this batch (JSON array stored as text)
    player_ids_json: str = Field(
        sa_column=Column(Text, nullable=False),
        description="JSON array of player IDs in submission order",
    )

    # Generation settings (needed to rebuild S3 keys on retrieve)
    style: str = Field(description="Image style used for this batch")
    image_size: str = Field(description="Image size setting: '512', '1K', '2K'")
    fetch_likeness: bool = Field(
        default=False,
        description="Whether likeness references were used",
    )

    # Timestamps
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = Field(
        default=None,
        description="When the batch job finished (success or failure)",
    )

    # Results summary (populated on retrieve)
    total_requests: int = Field(description="Number of image requests in batch")
    success_count: Optional[int] = Field(
        default=None,
        description="Number of successful generations",
    )
    failure_count: Optional[int] = Field(
        default=None,
        description="Number of failed generations",
    )

    # Error tracking
    error_message: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Error message if batch job failed",
    )

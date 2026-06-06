"""PlayerEnrichmentJob table — tracks on-demand enrichment requests.

Each row represents a single-player enrichment job, mirroring the
``ImageBatchJob`` state/index shape so the same queue-claim pattern
(``FOR UPDATE SKIP LOCKED``) works here too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel


class PlayerEnrichmentJob(SQLModel, table=True):  # type: ignore[call-arg]
    """Tracks on-demand player enrichment requests.

    One row per enrichment request. The worker claims ``queued`` rows,
    transitions them through ``running`` → ``succeeded`` / ``failed``,
    and stamps timestamps at each transition.
    """

    __tablename__ = "player_enrichment_jobs"
    __table_args__ = (
        # FIFO queue claim: WHERE state = 'queued' ORDER BY created_at
        Index("ix_enrichment_jobs_state_created", "state", "created_at"),
        # Status polling by player: WHERE player_id = ANY(...)
        Index("ix_enrichment_jobs_player", "player_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Which player is being enriched
    player_id: int = Field(foreign_key="players_master.id")

    # Job lifecycle state: queued | running | succeeded | failed
    state: str = Field(default="queued")

    # Who/what triggered this job: admin_single | admin_bulk | cron
    source: str

    # Null for cron-triggered jobs
    requested_by_user_id: Optional[int] = Field(
        default=None, foreign_key="auth_users.id"
    )

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)

    # Populated on failure
    error_message: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

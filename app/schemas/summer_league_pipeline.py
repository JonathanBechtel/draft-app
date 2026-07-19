"""Operational state for the coordinated Summer League write pipeline.

These rows are operational projections, not player-data assertions.  They make
the hand-off between the fast Desk tick and the broad ingestion runner durable:
a lower-priority runner that cannot obtain the shared writer lock leaves behind
an explicit reconciliation request which a later scheduled run must drain.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Enum as SAEnum, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class SummerLeaguePipelineJob(str, Enum):
    """A scheduled writer participating in the Summer League pipeline."""

    DESK = "desk"
    FULL_INGESTION = "full_ingestion"
    ENVIRONMENT_REFRESH = "environment_refresh"


class SummerLeaguePipelineOutcome(str, Enum):
    """Most recent observable outcome for a scheduled pipeline job."""

    SUCCEEDED = "succeeded"
    DEFERRED = "deferred"
    FAILED = "failed"


class SummerLeaguePipelineState(SQLModel, table=True):  # type: ignore[call-arg]
    """Durable freshness and retry state for one Summer League cron job.

    ``pending_reconciliation`` belongs to the full-ingestion job.  It is set
    when that lower-priority job yields to the Desk and only cleared after a
    later full metrics rebuild and snapshot materialization complete in order.
    """

    __tablename__ = "summer_league_pipeline_states"
    __table_args__ = (
        UniqueConstraint("job", name="uq_summer_league_pipeline_states_job"),
        Index("ix_summer_league_pipeline_states_pending", "pending_reconciliation"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    job: SummerLeaguePipelineJob = Field(
        sa_column=Column(
            SAEnum(
                SummerLeaguePipelineJob,
                name="summer_league_pipeline_job_enum",
            ),
            nullable=False,
        )
    )
    last_outcome: Optional[SummerLeaguePipelineOutcome] = Field(
        default=None,
        sa_column=Column(
            SAEnum(
                SummerLeaguePipelineOutcome,
                name="summer_league_pipeline_outcome_enum",
            ),
            nullable=True,
        ),
    )
    pending_reconciliation: bool = Field(default=False, nullable=False)
    consecutive_deferrals: int = Field(default=0, nullable=False)
    last_deferred_at: Optional[datetime] = Field(default=None)
    last_succeeded_at: Optional[datetime] = Field(default=None)
    last_metrics_rebuilt_at: Optional[datetime] = Field(default=None)
    last_snapshots_materialized_at: Optional[datetime] = Field(default=None)
    last_failure_at: Optional[datetime] = Field(default=None)
    last_failure_reason: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

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


class SummerLeagueBatchPhase(str, Enum):
    """A per-game normalization phase tracked for batch-resumability.

    Backbone normalization is not represented here -- it runs in one
    ``db.begin()`` block per venue per run (not chunked into game-id
    batches) and is already fully idempotent on retry, so it needs no
    durable per-game progress marker. Shot and PBP normalization *are*
    chunked into small game-id batches (see
    ``app.cli.summer_league_ingest_runner``), so a crash/interruption
    partway through a venue needs to know exactly which games already
    committed for each of these two phases.
    """

    SHOT = "shot"
    PBP = "pbp"


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


class SummerLeagueBatchProgress(SQLModel, table=True):  # type: ignore[call-arg]
    """Durable per-game completion marker for one batched normalization phase.

    Each row records that ``game_id`` (an ``nba_stats_game_id``) has been
    successfully committed for ``phase`` within one ``year``/``league_id``
    slice. ``app.cli.summer_league_ingest_runner`` reads this set before
    planning a phase's batches so a crash/interruption partway through a
    venue's shot/PBP normalization resumes only the games that were not yet
    committed, instead of replaying an entire venue inside one long-lived
    advisory-lock transaction (the 87.7-minute production incident this
    ticket exists to prevent -- see
    ``docs/plans/summer-league-cron-desk-starvation-spec.md``).

    Rows are durable but not permanent: on a routine run, a game already
    marked complete here is skipped on every later run, so only
    newly-discovered games are ever normalized again -- giving routine runs
    a free "changed games only" property. But should a completed game's raw
    snapshot later change (a forced re-fetch correcting a bad box score, a
    corrected PBP/shot-chart snapshot, or an explicit repair run), its row
    must be deleted first via
    ``app.services.summer_league.batch_progress.invalidate_batch_progress``
    -- see ``app.services.summer_league.raw_ingestion.dirty_game_ids_from_manifest``
    and ``app.cli.summer_league_ingest_runner``'s dirty-detection wiring --
    or that game would otherwise be silently skipped forever.
    """

    __tablename__ = "summer_league_batch_progress"
    __table_args__ = (
        UniqueConstraint(
            "year",
            "league_id",
            "phase",
            "game_id",
            name="uq_summer_league_batch_progress_game",
        ),
        Index(
            "ix_summer_league_batch_progress_scope",
            "year",
            "league_id",
            "phase",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    year: int = Field(nullable=False)
    league_id: str = Field(nullable=False, max_length=8)
    phase: SummerLeagueBatchPhase = Field(
        sa_column=Column(
            SAEnum(
                SummerLeagueBatchPhase,
                name="summer_league_batch_phase_enum",
            ),
            nullable=False,
        )
    )
    game_id: str = Field(nullable=False, max_length=32)
    completed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

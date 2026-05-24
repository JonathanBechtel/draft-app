"""Phase 2 consensus tables.

Snapshots are append-only: each recompute writes a new ``ConsensusSnapshot``
row plus a fresh batch of ``BigBoardConsensus`` (per-player) and
``SourceAnalytics`` (per-source) rows. Earlier snapshots stay in place so
the rank-history chart in Phase 3 falls out of a simple query.

See ``docs/consensus_phase_2_design.md`` for the algorithm and rationale.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ConsensusTrigger(str, Enum):
    """What caused a consensus recompute to run.

    Stored on the snapshot for audit so we can answer "did this snapshot
    fire because a board got approved, or because the daily cron ran?".
    """

    BOARD_APPROVED = "BOARD_APPROVED"
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"


class ConsensusSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    """Groups one consensus computation pass for a given draft year.

    A snapshot owns the ``BigBoardConsensus`` rows produced by that
    pass plus the ``SourceAnalytics`` rows describing how each source
    deviated from consensus on that pass. Append-only — never updated.
    """

    __tablename__ = "consensus_snapshots"
    __table_args__ = (
        Index(
            "ix_consensus_snapshots_year_computed",
            "draft_year",
            "computed_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    draft_year: int = Field(index=True)
    computed_at: datetime = Field(
        default_factory=datetime.utcnow,
        index=True,
        description="When this snapshot was computed.",
    )
    num_boards: int = Field(description="How many APPROVED boards fed this snapshot.")
    board_ids: list[int] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False),
        description=(
            "Big board ids that were included in this snapshot. Stored "
            "as a JSONB list so we can answer 'which sources fed this "
            "snapshot?' without joining through entries."
        ),
    )
    trigger: ConsensusTrigger = Field(
        sa_column=Column(
            SAEnum(ConsensusTrigger, name="consensus_trigger_enum"),
            nullable=False,
        )
    )


class BigBoardConsensus(SQLModel, table=True):  # type: ignore[call-arg]
    """One per-player row in a consensus snapshot.

    ``consensus_rank`` is the 1-based final ordering for the snapshot;
    the other columns are the underlying aggregation (avg, median,
    high/low, std_dev, num_sources) plus the previous-snapshot delta
    that lets Phase 3 render risers and fallers.
    """

    __tablename__ = "big_board_consensus"
    __table_args__ = (
        # Each player appears at most once per snapshot, and each
        # consensus_rank slot is held by exactly one player (the
        # algorithm resolves ties so the final ordering is strict).
        UniqueConstraint(
            "snapshot_id",
            "player_id",
            name="uq_big_board_consensus_snapshot_player",
        ),
        UniqueConstraint(
            "snapshot_id",
            "consensus_rank",
            name="uq_big_board_consensus_snapshot_rank",
        ),
        Index(
            "ix_big_board_consensus_snapshot_rank",
            "snapshot_id",
            "consensus_rank",
        ),
        Index(
            "ix_big_board_consensus_player_snapshot",
            "player_id",
            "snapshot_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("consensus_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    draft_year: int = Field(index=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)

    consensus_rank: int = Field(
        description="1-based final position on the consensus board."
    )
    avg_rank: float = Field(
        description="Mean rank across sources that ranked this player."
    )
    median_rank: float = Field(description="Median rank across sources.")
    high_rank: int = Field(
        description="Best (lowest-number) rank any source gave this player."
    )
    low_rank: int = Field(description="Worst (highest-number) rank any source gave.")
    std_dev: float = Field(description="Std dev of source ranks; 0 when num_sources=1.")
    num_sources: int = Field(
        description="Count of eligible boards that ranked this player."
    )

    prev_rank: Optional[int] = Field(
        default=None,
        description="consensus_rank from the previous snapshot for this draft year.",
    )
    rank_delta: Optional[int] = Field(
        default=None,
        description="prev_rank - consensus_rank (positive = rising).",
    )


class SourceAnalytics(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-source deviation metrics for a single consensus snapshot.

    ``contrarian_score`` is a z-score of ``avg_deviation`` across all
    sources in the same snapshot, so it stays meaningful as more
    sources come online.
    """

    __tablename__ = "source_analytics"
    __table_args__ = (
        # Exactly one analytics row per (snapshot, source). Without this,
        # a retried recompute could double-count a source in downstream
        # contrarian/deviation reporting.
        UniqueConstraint(
            "snapshot_id",
            "news_source_id",
            name="uq_source_analytics_snapshot_source",
        ),
        Index(
            "ix_source_analytics_snapshot",
            "snapshot_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("consensus_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    news_source_id: int = Field(foreign_key="news_sources.id", index=True)
    latest_board_id: int = Field(
        foreign_key="big_boards.id",
        description="Which board of this source was used in this snapshot.",
    )

    avg_deviation: float = Field(
        description=(
            "Mean absolute rank-distance from consensus across the "
            "players this source ranked."
        )
    )
    contrarian_score: float = Field(
        description=(
            "Z-score of avg_deviation across all sources in this "
            "snapshot. Positive = more contrarian than average."
        )
    )
    biggest_outlier_player_id: Optional[int] = Field(
        default=None,
        foreign_key="players_master.id",
        description="Player where this source diverges most from consensus.",
    )
    outlier_delta: int = Field(
        default=0,
        description=(
            "Signed delta on the biggest_outlier_player: "
            "consensus_rank - source_rank (positive = source higher)."
        ),
    )

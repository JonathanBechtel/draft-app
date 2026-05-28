"""Pydantic response models for the public consensus read API."""

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel

from app.schemas.consensus import ConsensusTrigger


class ConsensusRow(SQLModel):
    """One player's row in a consensus snapshot.

    Returned by GET /api/consensus for the board-level view.
    """

    player_id: int
    player_name: Optional[str]
    school: Optional[str]
    slug: Optional[str] = None
    photo_url: Optional[str] = None
    school_logo_url: Optional[str] = None
    consensus_rank: int
    avg_rank: float
    median_rank: float
    high_rank: int
    low_rank: int
    std_dev: float
    num_sources: int
    prev_rank: Optional[int] = None
    rank_delta: Optional[int] = None


class RankHistoryPoint(SQLModel):
    """One snapshot's rank for a single player — used in trajectory charts."""

    computed_at: datetime
    consensus_rank: int
    snapshot_id: int


class SourceRankEntry(SQLModel):
    """A single source's rank/pick for a player in the per-source breakdown."""

    news_source_id: int
    source_name: str
    source_display_name: str
    source_rank: int


class PlayerConsensusDetail(SQLModel):
    """Full consensus detail for one player.

    Returned by GET /api/consensus/player/{player_id}.
    Includes the current consensus row, per-source breakdown, and rank history.
    """

    player_id: int
    player_name: Optional[str]
    school: Optional[str]
    consensus_rank: int
    avg_rank: float
    median_rank: float
    high_rank: int
    low_rank: int
    std_dev: float
    num_sources: int
    prev_rank: Optional[int] = None
    rank_delta: Optional[int] = None
    source_ranks: list[SourceRankEntry]
    rank_history: list[RankHistoryPoint]


class SourceAnalyticsRow(SQLModel):
    """Per-source deviation analytics for a consensus snapshot.

    Returned by GET /api/consensus/sources.
    """

    id: int
    snapshot_id: int
    news_source_id: int
    source_name: str
    source_display_name: str
    latest_board_id: int
    avg_deviation: float
    contrarian_score: float
    biggest_outlier_player_id: Optional[int] = None
    outlier_delta: int


class SnapshotSummary(SQLModel):
    """Lightweight summary of a consensus snapshot.

    Returned by GET /api/consensus/snapshots.
    """

    id: int
    draft_year: int
    computed_at: datetime
    num_boards: int
    trigger: ConsensusTrigger

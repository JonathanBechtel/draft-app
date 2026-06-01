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
    age: Optional[float] = None
    # Physical profile (best-effort from PlayerStatus; None when unknown).
    position: Optional[str] = None  # e.g. "PG/SG", "C"
    height: Optional[str] = None  # formatted feet'inches", e.g. "6'9\""
    weight: Optional[int] = None  # pounds
    consensus_rank: int
    avg_rank: float
    median_rank: float
    high_rank: int
    low_rank: int
    std_dev: float
    num_sources: int
    prev_rank: Optional[int] = None
    rank_delta: Optional[int] = None
    # Oldest-to-newest series of this player's consensus_rank across recent
    # snapshots; consumers render it as a sparkline. Empty when no history.
    recent_ranks: list[int] = []


class MockConsensusRow(ConsensusRow):
    """A consensus row with its draft slot's owning team overlaid.

    Used by the post-lottery mock-draft presentation: the player at
    ``consensus_rank`` N is shown alongside the team that owns overall pick N.
    The ranking is unchanged — this only attaches the team at each slot.

    Team fields are ``None`` when the consensus rank has no matching pick slot
    (the consensus runs deeper than the seeded order, or the order is not
    seeded for the year); the UI renders those rows without a team chip.
    """

    overall_pick: Optional[int] = None
    round: Optional[int] = None
    team_name: Optional[str] = None
    team_abbreviation: Optional[str] = None
    team_slug: Optional[str] = None
    team_logo_url: Optional[str] = None
    team_primary_color: Optional[str] = None
    # The pick's original owner when acquired via trade; rendered as a small
    # "via {abbr}" subscript next to the (bold) current owner.
    original_team_abbreviation: Optional[str] = None
    trade_note: Optional[str] = None


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
    # The source article (mock/board) this rank was extracted from, when the
    # contributing board was backed by a real NewsItem. ``None`` for synthetic
    # or non-extracted boards — the UI renders the source name as plain text.
    article_url: Optional[str] = None
    article_title: Optional[str] = None


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

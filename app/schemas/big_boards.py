"""Big Board tables: per-source talent rankings and their entries."""

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
from sqlmodel import Field, SQLModel


class BoardStatus(str, Enum):
    """Lifecycle status for an ingested or manually entered big board."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BigBoard(SQLModel, table=True):  # type: ignore[call-arg]
    """A single analyst's talent ranking of prospects for a given draft year.

    One row per published board. Entries (the ranked players) live in
    ``BigBoardEntry``. A board is in ``PENDING`` status until an admin
    reviews it; after ``APPROVED`` it becomes immutable so it cannot be
    edited to misrepresent the analyst's actual rankings.
    """

    __tablename__ = "big_boards"
    __table_args__ = (
        Index("ix_big_boards_status_draft_year", "status", "draft_year"),
        Index("ix_big_boards_source_draft_year", "news_source_id", "draft_year"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    news_source_id: int = Field(foreign_key="news_sources.id", index=True)
    news_item_id: Optional[int] = Field(
        default=None,
        foreign_key="news_items.id",
        description="Link to the originating article when available.",
    )
    draft_year: int = Field(index=True)
    published_at: datetime = Field(
        index=True,
        description="When the analyst published the board (not when we ingested it).",
    )
    board_size: int = Field(description="Number of ranked players on the board.")

    status: BoardStatus = Field(
        default=BoardStatus.PENDING,
        sa_column=Column(
            SAEnum(BoardStatus, name="board_status_enum"),
            nullable=False,
            server_default=BoardStatus.PENDING.value,
            index=True,
        ),
    )
    approved_at: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BigBoardEntry(SQLModel, table=True):  # type: ignore[call-arg]
    """A single (player, rank) row on a ``BigBoard``."""

    __tablename__ = "big_board_entries"
    __table_args__ = (
        UniqueConstraint("board_id", "rank", name="uq_big_board_entries_board_rank"),
        UniqueConstraint(
            "board_id", "player_id", name="uq_big_board_entries_board_player"
        ),
        Index("ix_big_board_entries_player", "player_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("big_boards.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    player_id: int = Field(foreign_key="players_master.id")
    rank: int = Field(description="Analyst's talent-ranking position (1-based).")
    tier: Optional[int] = Field(
        default=None, description="Optional tier grouping assigned by the analyst."
    )

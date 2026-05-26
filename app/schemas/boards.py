"""Unified Board schema covering both big boards and mock drafts.

A ``Board`` is one analyst's published ranking for a draft year. The
``kind`` enum discriminates between BIG_BOARD (talent rankings) and
MOCK_DRAFT (pick-slot predictions). Both share the same lifecycle
(PENDING → APPROVED / REJECTED), validation rules, autosave UX, and
admin entry flow; mock drafts use additional per-entry fields
(``round``, ``team_id``, ``original_team_id``, ``trade_note``).

See ``docs/consensus_mock_plan.md`` for the unification rationale.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class BoardStatus(str, Enum):
    """Lifecycle status for an ingested or manually entered board."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BoardKind(str, Enum):
    """Discriminator for what kind of ranking a board represents."""

    BIG_BOARD = "BIG_BOARD"
    MOCK_DRAFT = "MOCK_DRAFT"


class ResolutionMethod(str, Enum):
    """How a raw player name was resolved (or not) to a ``players_master`` row.

    Values:
        EXACT: Normalized name matched a ``players_master.display_name`` exactly.
        ALIAS: Matched via a ``player_aliases`` row.
        VECTOR: Matched by embedding nearest-neighbour search.
        MANUAL: Resolved by a human admin via the resolution UI.
        STUB: Auto-created a stub ``players_master`` row for an unrecognised name.
        UNRESOLVED: Not yet resolved; awaiting human review.
    """

    EXACT = "EXACT"
    ALIAS = "ALIAS"
    VECTOR = "VECTOR"
    MANUAL = "MANUAL"
    STUB = "STUB"
    UNRESOLVED = "UNRESOLVED"


class Board(SQLModel, table=True):  # type: ignore[call-arg]
    """A single analyst's published big board or mock draft."""

    __tablename__ = "boards"
    __table_args__ = (
        Index("ix_boards_status_draft_year", "status", "draft_year"),
        Index("ix_boards_source_draft_year", "news_source_id", "draft_year"),
        Index("ix_boards_kind_draft_year", "kind", "draft_year"),
        # Enforce kind-specific shape at the DB level so a buggy admin
        # path can't end up with a "big board" that has num_rounds, or a
        # mock draft missing it.
        CheckConstraint(
            "(kind = 'BIG_BOARD' AND num_rounds IS NULL) "
            "OR (kind = 'MOCK_DRAFT' AND num_rounds IS NOT NULL)",
            name="ck_boards_kind_num_rounds",
        ),
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
        description="When the analyst published the board (not when we ingested).",
    )
    size: int = Field(
        description="Number of ranked players (big board) or picks (mock draft)."
    )

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

    kind: BoardKind = Field(
        sa_column=Column(
            SAEnum(BoardKind, name="board_kind_enum"),
            nullable=False,
            server_default=BoardKind.BIG_BOARD.value,
            index=True,
        ),
    )
    num_rounds: Optional[int] = Field(
        default=None,
        description="MOCK_DRAFT only: number of rounds (1 or 2).",
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BoardEntry(SQLModel, table=True):  # type: ignore[call-arg]
    """A single ranked player on a ``Board``.

    For ``BIG_BOARD`` boards, ``position`` is the analyst's rank.
    For ``MOCK_DRAFT`` boards, ``position`` is the overall pick number
    (1–60) and ``round``, ``team_id``, ``original_team_id``,
    ``trade_note`` carry the additional pick-specific context.

    Entity resolution fields:
        raw_name: The exact name string the AI emitted, stored verbatim
            so it can be re-evaluated or audited later.
        resolution_method: How (or whether) ``raw_name`` was resolved to
            ``player_id``.  UNRESOLVED rows have ``player_id=None``.
        vector_candidates: JSONB list of ``{player_id, display_name,
            score}`` produced by the embedding nearest-neighbour pass,
            preserved for admin review.
    """

    __tablename__ = "board_entries"
    __table_args__ = (
        UniqueConstraint(
            "board_id", "position", name="uq_board_entries_board_position"
        ),
        UniqueConstraint("board_id", "player_id", name="uq_board_entries_board_player"),
        Index("ix_board_entries_player", "player_id"),
        Index("ix_board_entries_resolution_method", "resolution_method"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("boards.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    player_id: Optional[int] = Field(
        default=None,
        foreign_key="players_master.id",
        description="Resolved player; NULL while resolution_method is UNRESOLVED.",
    )
    position: int = Field(
        description="Rank (big board) or overall pick number (mock draft); 1-based."
    )

    # Entity-resolution fields.
    raw_name: str = Field(
        default="",
        description="Verbatim player name emitted by the extraction AI.",
    )
    resolution_method: ResolutionMethod = Field(
        default=ResolutionMethod.UNRESOLVED,
        sa_column=Column(
            SAEnum(ResolutionMethod, name="resolution_method_enum"),
            nullable=False,
            server_default=ResolutionMethod.UNRESOLVED.value,
        ),
    )
    vector_candidates: Optional[list[dict]] = Field(  # type: ignore[type-arg]
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description=(
            "Nearest-neighbour candidates from the vector pass: "
            "[{player_id, display_name, score}, ...]"
        ),
    )

    # Big-board-only field.
    tier: Optional[int] = Field(
        default=None, description="BIG_BOARD only: optional tier grouping."
    )

    # Mock-draft-only fields.
    round: Optional[int] = Field(default=None, description="MOCK_DRAFT only: 1 or 2.")
    team_id: Optional[int] = Field(
        default=None,
        foreign_key="nba_teams.id",
        description="MOCK_DRAFT only: team making the selection.",
    )
    original_team_id: Optional[int] = Field(
        default=None,
        foreign_key="nba_teams.id",
        description="MOCK_DRAFT only: original pick owner if traded.",
    )
    trade_note: Optional[str] = Field(
        default=None,
        description="MOCK_DRAFT only: e.g., 'via trade with PHX'.",
    )

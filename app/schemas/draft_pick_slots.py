"""Canonical draft-order reference table.

A ``DraftPickSlot`` records who owns each overall pick of a draft year —
canonical, static, public data set once the lottery fixes the order. It is
identical across every analyst's mock, so we maintain it once here and overlay
it on the unified consensus ranking at render time (``consensus_rank N → slot N
→ owning team``) to produce the post-lottery mock-draft presentation.

This table deliberately carries NO ranking signal: the consensus engine never
reads it. See ``docs/draft_order_reference_plan.md`` for the end-to-end design.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class DraftPickSlot(SQLModel, table=True):  # type: ignore[call-arg]
    """One overall pick of a draft year and the team that currently owns it.

    ``team_id`` is the current owner of the pick; ``original_team_id`` records
    the original owner when the pick was acquired via trade, and ``trade_note``
    holds a short human label (e.g. ``"via IND"``) for display.
    """

    __tablename__ = "draft_pick_slots"
    __table_args__ = (
        UniqueConstraint(
            "draft_year",
            "overall_pick",
            name="uq_draft_pick_slots_year_overall",
        ),
        UniqueConstraint(
            "draft_year",
            "round",
            "round_pick",
            name="uq_draft_pick_slots_year_round_pick",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    draft_year: int = Field(index=True)
    overall_pick: int = Field(description="1-based overall pick number (1..60).")
    round: int = Field(description="Draft round (1 or 2).")
    round_pick: int = Field(description="Pick number within the round (1..30).")
    team_id: int = Field(
        foreign_key="nba_teams.id",
        description="Current owner of the pick.",
    )
    original_team_id: Optional[int] = Field(
        default=None,
        foreign_key="nba_teams.id",
        description="Original owner of the pick when acquired via trade.",
    )
    trade_note: Optional[str] = Field(
        default=None,
        description="Short trade label for display, e.g. 'via IND'.",
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

"""Actual draft-outcome table.

A ``DraftResult`` records what *actually* happened on draft night: which
player was selected at each overall pick, and by which team. It is the
post-draft counterpart to the pre-draft signal stored in
``big_board_consensus`` — joining the two (``overall_pick`` vs.
``consensus_rank``) is what powers every "reach / steal / vs. expectations"
view on the draft-recap pages.

Unlike ``draft_pick_slots`` (the static, pre-draft *order* of who owns each
slot), this table is written as the picks come in on draft night. ``player_id``
and ``team_id`` are nullable so an unmatched pick can still be recorded and
routed to manual review rather than dropped; ``raw_player_name`` / ``raw_team``
always preserve the verbatim input for audit.

See ``docs/plans/summer-league-data-backbone.md`` for the broader
event-sourced lifecycle this feeds into.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class DraftResult(SQLModel, table=True):  # type: ignore[call-arg]
    """One actual selection of a draft year: pick slot → player → team.

    ``player_id`` / ``team_id`` are the resolved references; they are nullable
    so a pick whose player name could not be matched (or whose match was
    ambiguous) is still recorded for manual review. ``raw_player_name`` and
    ``raw_team`` keep the verbatim input from ingestion regardless.
    """

    __tablename__ = "draft_results"
    __table_args__ = (
        UniqueConstraint(
            "draft_year",
            "overall_pick",
            name="uq_draft_results_year_overall",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    draft_year: int = Field(index=True)
    overall_pick: int = Field(description="1-based overall pick number (1..60).")
    round: int = Field(description="Draft round (1 or 2).")
    round_pick: int = Field(description="Pick number within the round (1..30).")

    player_id: Optional[int] = Field(
        default=None,
        foreign_key="players_master.id",
        index=True,
        description="Resolved player; NULL when the name could not be matched.",
    )
    team_id: Optional[int] = Field(
        default=None,
        foreign_key="nba_teams.id",
        description="Selecting team; NULL when the abbreviation could not be matched.",
    )

    raw_player_name: str = Field(
        description="Verbatim player name from ingestion input.",
    )
    raw_team: Optional[str] = Field(
        default=None,
        description="Verbatim team token (abbreviation) from ingestion input.",
    )
    resolution_method: str = Field(
        default="unresolved",
        description=(
            "How player_id was resolved: 'matched', 'manual', or 'unresolved'."
        ),
    )

    picked_at: Optional[datetime] = Field(
        default=None,
        description="When the pick was announced, if known.",
    )
    source: str = Field(
        default="manual",
        description="Where the result came from, e.g. 'manual' or 'espn'.",
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

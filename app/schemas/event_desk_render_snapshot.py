"""EventDeskRenderSnapshot -- the generic, persistent Desk render-snapshot projection.

Summer League Desk launch-readiness plan (`docs/plans/summer-league-desk-launch-readiness.md`,
work-breakdown item 9 "Render snapshot persistence") calls out that the homepage must not
rebuild an hourly read model on every visitor request: a cold Fly worker should load a
complete, already-typed Desk view without re-running the full read-model assembly
(`app.services.summer_league.desk_read._assemble_desk_payload`) query-by-query.

This table is that persisted projection. One row is one fully-materialized render
**variant** -- keyed by the registered :class:`~app.schemas.event_desk.Event`, the daily
state it was built for (Preview / Live / Recap), and the Class Tracker cohort + stat-view
toggle combination it was built with. The hourly Event Desk tick (materialization ticket,
launch-readiness item 10) upserts every variant it needs; a request-time read then does a
single exact lookup by those four keys instead of reassembling anything.

Like `summer_league_desk` (T1-T4) and `event_desk_state`, this is a **rebuildable read-model
projection**, never a source of truth -- safe to truncate and rematerialize from canonical
assertions plus the T1-T4 Desk projections at any time. It holds a schema-versioned,
JSON-encoded copy of :class:`~app.services.summer_league.desk_read.DeskView` (the payload
plus its player/matchup/team view-context enrichment) so a reader never has to know the
Python dataclass shape changed underneath an old row -- `schema_version` lets the codec
(`app.services.event_desk.render_snapshots`) refuse to silently misinterpret a stale
payload shape instead of raising a typed error.

``source_freshness_tick_at`` / ``source_freshness_next_tick_eta`` mirror the upstream
`event_desk_state` freshness stamp at the moment this variant was materialized, so a reader
can render an honest "as of" label without a second query back to `event_desk_state`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.schemas.event_desk import EventDailyState


def _enum_column(enum_cls: type[Enum], name: str) -> Column:
    """Build a Postgres-backed SAEnum column that serializes on member value.

    Reuses the exact ``name`` the enum type already carries in Postgres
    (``event_daily_state_enum``, created by `app.schemas.event_desk.EventDeskState`)
    rather than minting a second, duplicate enum type for the same Python enum.
    """
    return Column(
        SAEnum(
            enum_cls,
            name=name,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
    )


class EventDeskRenderSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    """One materialized, fully-typed Desk render variant for cold/fast request-time reads.

    Uniquely keyed by ``(event_id, daily_state, tracker_cohort, tracker_stat_view)`` --
    every supported variant coexists as its own row and is upserted independently by the
    tick (a Live-state rewrite never touches the Preview/Recap rows for the same event).
    """

    __tablename__ = "event_desk_render_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "daily_state",
            "tracker_cohort",
            "tracker_stat_view",
            name="uq_event_desk_render_snapshots_variant",
        ),
        # Lookup path for "give me every variant currently stored for this event's
        # daily state" (e.g. tick-time enumeration / admin inspection) without
        # requiring the cohort/stat-view keys the exact-read repository op needs.
        Index(
            "ix_event_desk_render_snapshots_event_daily_state",
            "event_id",
            "daily_state",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id")
    daily_state: EventDailyState = Field(
        sa_column=_enum_column(EventDailyState, "event_daily_state_enum")
    )
    # Class Tracker toggle state this variant was built with -- one of
    # `app.services.summer_league.desk_read.TRACKER_COHORTS` /
    # `TRACKER_STAT_VIEWS`. Kept as plain str (not an enum FK) here: the render
    # snapshot table is framework-level and must not hard-code SL's specific
    # cohort/stat-view vocabulary the way `summer_league_desk` may.
    tracker_cohort: str = Field(nullable=False)
    tracker_stat_view: str = Field(nullable=False)

    # Codec version the JSON columns below were encoded with (see
    # `app.services.event_desk.render_snapshots.CURRENT_SCHEMA_VERSION`). A reader
    # must reject a row whose version it doesn't understand rather than guess.
    schema_version: int = Field(nullable=False)
    # The encoded `DeskPayload` (`app.services.event_desk.payload.DeskPayload`), or
    # null when the event has no in-window payload for this variant (should not
    # normally be persisted, but the column stays nullable so a defensive
    # off-window write never violates a NOT NULL constraint).
    payload_json: Optional[dict[str, object]] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # The encoded player/matchup/tracker-team view-context enrichment
    # (`get_desk_view_context` + `_assemble_tracker`'s team lookup).
    view_context_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )

    # Copied from `event_desk_state` at materialization time (see module docstring).
    source_freshness_tick_at: Optional[datetime] = Field(default=None)
    source_freshness_next_tick_eta: Optional[datetime] = Field(default=None)

    # When this specific variant row was last (re)written -- distinct from
    # `source_freshness_tick_at`, which is the upstream tick's own stamp.
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

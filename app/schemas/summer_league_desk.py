"""Summer League Desk projection tables (T1-T4).

The Summer League Desk (`docs/plans/summer-league-scouts-desk-behavior-spec.md`, §10) is
event-instance #1 of the generic Event Desk framework
(`docs/plans/event-desk-framework.md`). These four tables are **rebuildable read-model
projections** — safe to drop and recompute from raw logs, draft slot, and consensus
assertions — never sources of truth:

* :class:`SummerLeagueCohortBaseline` (T1) — the precomputed, versioned distribution
  per draft-slot/status cohort (Job A, offline/rare refresh). Mirrors the
  ``summer_league_metric_models`` versioned pattern (``*_version`` + ``is_active``).
* :class:`SummerLeagueDeskPlayerGrade` (T2) — one row per active player per event per
  baseline version: subject value, percentile vs cohort, grade, and selected rendered
  commentary Facts (Job B, hourly tick).
* :class:`SummerLeagueDeskStoryline` (T3) — one row per fired storyline trigger per game
  (Debut / Duel / Streak / Status heat / 2nd look), with base/magnitude/weight.
* :class:`SummerLeagueDeskSlate` (T4) — one row per game per day: the summed storyline
  weight, rank, hero flag, and selected rendered commentary Facts for cheap slate reads.

Framework-level state (lifecycle phase, daily state, freshness) lives in the generic
``event_desk_state`` table (out of scope here) — these tables hold SL-specific content
only.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class SummerLeagueDeskCohortKind(str, Enum):
    """Kind of cohort a baseline (T1) row groups players by."""

    SLOT_WINDOW = "slot_window"
    ROUND_BUCKET = "round_bucket"
    STATUS = "status"
    DEBUT = "debut"


class SummerLeagueDeskGrain(str, Enum):
    """Aggregation grain a baseline (T1) distribution was fit over."""

    EVENT = "event"
    GAME = "game"
    DEBUT = "debut"


class SummerLeagueDeskGrade(str, Enum):
    """Coarse percentile-to-grade bucket for a player grade (T2) row."""

    HOT = "hot"
    WARM = "warm"
    MID = "mid"
    COLD = "cold"


class SummerLeagueDeskTriggerType(str, Enum):
    """The five storyline engine trigger types (behavior spec §3)."""

    DEBUT = "debut"
    DUEL = "duel"
    STREAK = "streak"
    STATUS_HEAT = "status_heat"
    SECOND_LOOK = "second_look"


def _enum_column(enum_cls: type[Enum], name: str) -> Column:
    """Build a Postgres-backed SAEnum column that serializes on member value."""
    return Column(
        SAEnum(
            enum_cls,
            name=name,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
    )


class SummerLeagueCohortBaseline(SQLModel, table=True):  # type: ignore[call-arg]
    """T1 — one precomputed, versioned cohort distribution (the expensive artifact).

    Refreshed rarely (new-history ingest / window-rule change / manual) by Job A
    (``scripts/build_sl_cohort_baselines.py``); never rebuilt on the hourly tick.
    """

    __tablename__ = "summer_league_cohort_baselines"
    __table_args__ = (
        UniqueConstraint(
            "baseline_version",
            "cohort_key",
            name="uq_summer_league_cohort_baselines_version_cohort",
        ),
        Index(
            "ix_summer_league_cohort_baselines_cohort_active",
            "cohort_key",
            "is_active",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    baseline_version: str = Field(nullable=False)
    is_active: bool = Field(default=True, nullable=False)

    # e.g. "slot:1-4", "round:1_late", "round:2", "status:undrafted", "debut:1-4".
    cohort_key: str = Field(nullable=False)
    cohort_kind: SummerLeagueDeskCohortKind = Field(
        sa_column=_enum_column(
            SummerLeagueDeskCohortKind, "summer_league_desk_cohort_kind_enum"
        )
    )
    slot_low: Optional[int] = Field(default=None)
    slot_high: Optional[int] = Field(default=None)

    metric: str = Field(default="gmsc", nullable=False)
    grain: SummerLeagueDeskGrain = Field(
        sa_column=_enum_column(SummerLeagueDeskGrain, "summer_league_desk_grain_enum")
    )
    venue_scope: str = Field(default="all", nullable=False)
    season_range: str = Field(nullable=False)

    min_minutes: float = Field(default=0.0, nullable=False)
    n_members: int = Field(default=0, nullable=False)

    # Percentile (as string key, e.g. "50", "90") -> metric value, for O(1) ranking.
    breakpoints: dict[str, float] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    mean_value: float = Field(default=0.0, nullable=False)
    median_value: float = Field(default=0.0, nullable=False)

    computed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueDeskPlayerGrade(SQLModel, table=True):  # type: ignore[call-arg]
    """T2 — per-event percentile sidecar; one row per active player per event.

    Keeps the canonical ``summer_league_player_seasons`` aggregate clean. Refreshed on
    the hourly tick (Job B).
    """

    __tablename__ = "summer_league_desk_player_grades"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "competition_id",
            "baseline_version",
            name="uq_summer_league_desk_player_grades_player_competition_version",
        ),
        Index(
            "ix_summer_league_desk_player_grades_competition_cohort",
            "competition_id",
            "cohort_key",
        ),
        Index(
            "ix_summer_league_desk_player_grades_player_id",
            "player_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id")
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    baseline_version: str = Field(nullable=False)
    cohort_key: str = Field(nullable=False)

    subject_value: float = Field(nullable=False)
    pctl: float = Field(nullable=False)
    grade: SummerLeagueDeskGrade = Field(
        sa_column=_enum_column(SummerLeagueDeskGrade, "summer_league_desk_grade_enum")
    )
    n_cohort: int = Field(default=0, nullable=False)
    # True when the adaptive gate ladder suppressed a confident percentile.
    gated: bool = Field(default=False, nullable=False)

    # Selected Fact objects + rendered strings for this player (spec §11 storage).
    facts: Optional[list[dict[str, object]]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    computed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueDeskStoryline(SQLModel, table=True):  # type: ignore[call-arg]
    """T3 — one row per fired storyline trigger instance for a game.

    A game may carry several rows (multiple badges). Written by the hourly tick (Job B).
    """

    __tablename__ = "summer_league_desk_storylines"
    __table_args__ = (
        Index(
            "ix_summer_league_desk_storylines_date_competition",
            "game_date",
            "competition_id",
        ),
        Index(
            "ix_summer_league_desk_storylines_game_id",
            "game_id",
        ),
        Index(
            "ix_summer_league_desk_storylines_subject_player_id",
            "subject_player_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    game_date: date = Field(nullable=False)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    game_id: int = Field(foreign_key="summer_league_games.id")

    trigger_type: SummerLeagueDeskTriggerType = Field(
        sa_column=_enum_column(
            SummerLeagueDeskTriggerType, "summer_league_desk_trigger_type_enum"
        )
    )
    subject_player_id: int = Field(foreign_key="players_master.id")
    # Second subject for a `duel` trigger; null for single-subject triggers.
    subject_player_id_2: Optional[int] = Field(
        default=None, foreign_key="players_master.id"
    )

    base_weight: float = Field(nullable=False)
    magnitude: float = Field(nullable=False)
    weight: float = Field(nullable=False)
    # Null pre-tip (Morning expected weight); filled once the game goes live.
    realized_deviation: Optional[float] = Field(default=None)

    computed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueDeskSlate(SQLModel, table=True):  # type: ignore[call-arg]
    """T4 — per-game rollup of T3 weights for cheap slate reads + share.

    One row per game per day, upserted each hourly tick (Job B).
    """

    __tablename__ = "summer_league_desk_slate"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            name="uq_summer_league_desk_slate_game",
        ),
        Index(
            "ix_summer_league_desk_slate_date_competition",
            "game_date",
            "competition_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    game_date: date = Field(nullable=False)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    game_id: int = Field(foreign_key="summer_league_games.id")

    total_weight: float = Field(default=0.0, nullable=False)
    rank: int = Field(default=0, nullable=False)
    is_hero: bool = Field(default=False, nullable=False)

    # Selected Facts + rendered read/headline strings for this game (spec §11 storage).
    facts: Optional[list[dict[str, object]]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    computed_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

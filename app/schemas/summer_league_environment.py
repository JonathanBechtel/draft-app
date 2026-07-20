"""Competition Context (Summer League environment) profile schema.

These tables are **derived, versioned read models** — replaceable projections
over the provenance-bearing Summer League game/participation/identity/shot
spokes, not a canonical source of truth. Per the Global Player-Journey Graph
backbone, a rebuild (#617) recomputes them atomically from the raw spokes and
flips a new ``is_current`` version into place; readers keep the last good
version if a rebuild fails.

Two stable scope identities are published (never display labels as keys):

* ``season:<year>`` — a calendar year pooled across every normalized
  Summer League competition that year (``scope_kind = 'season_all_competitions'``).
* ``competition:<competition_id>`` — one named competition edition, isolated by
  canonical competition id (``scope_kind = 'competition'``).

Tables
------
* :class:`SummerLeagueEnvironmentProfile` — one versioned row per (scope, version)
  holding the typed, sortable/filterable v1 metric values plus identity/coverage
  and field-composition scalars. A partial unique index guarantees exactly one
  ``is_current`` row per ``scope_key``.
* :class:`SummerLeagueEnvironmentSeasonMembership` — the competitions pooled into
  a season profile (season scope only). Uniqueness prevents a competition from
  being counted twice in one season profile.
* :class:`SummerLeagueEnvironmentMetricCoverage` — per-metric ``complete`` /
  ``partial`` / ``unavailable`` verdict, covered/eligible game counts, and a
  human reason. ``partial`` is never coerced to zero and never certifies a
  metric.
* :class:`SummerLeagueEnvironmentFieldComposition` — per-attribute (draft / age /
  position / origin) known / unknown / total counts over resolved appeared
  players plus an optional distribution histogram.
* :class:`SummerLeagueEnvironmentProvenance` — per-source watermark, row count,
  and parse-status summary feeding the profile's freshness disclosure.

The exact metric formulas, denominators, rounding, coverage rules, and
event-time field-composition semantics live in
:mod:`app.services.summer_league_environment_registry`, which is the single
shared definition consumed by aggregation (#617) and the Explorer (#607/#608).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Stable scope-kind tokens (never display labels).
SCOPE_KIND_SEASON = "season_all_competitions"
SCOPE_KIND_COMPETITION = "competition"

# Coverage verdicts (mirror the registry / audit vocabulary).
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_UNAVAILABLE = "unavailable"


class SummerLeagueEnvironmentProfile(SQLModel, table=True):  # type: ignore[call-arg]
    """One versioned Competition Context profile for a season or competition scope.

    Typed numeric columns hold the sortable / filterable v1 metric values so
    Explorer can order and threshold without unpacking JSON. A metric value is
    ``NULL`` when its input coverage is not ``complete`` for the scope; a missing
    shot-chart rate never invalidates a box-derived rate. Field-composition
    *scalars* (counts and median age) are stored here; per-attribute coverage,
    per-metric coverage verdicts, season membership, and source provenance live
    in the child tables.
    """

    __tablename__ = "summer_league_environment_profiles"
    __table_args__ = (
        # No two versions share a (scope, version) pair.
        UniqueConstraint(
            "scope_key",
            "version",
            name="uq_sl_environment_profiles_scope_version",
        ),
        # Exactly one current profile per scope.
        Index(
            "uq_sl_environment_profiles_current",
            "scope_key",
            unique=True,
            postgresql_where=text("is_current = true"),
        ),
        # Scope listing / history reads.
        Index(
            "ix_sl_environment_profiles_kind_year",
            "scope_kind",
            "year",
        ),
        Index(
            "ix_sl_environment_profiles_competition",
            "competition_id",
        ),
        CheckConstraint(
            "scope_kind IN ('season_all_competitions', 'competition')",
            name="ck_sl_environment_profiles_scope_kind",
        ),
        CheckConstraint(
            "(scope_kind = 'competition' AND competition_id IS NOT NULL) "
            "OR (scope_kind = 'season_all_competitions' AND competition_id IS NULL)",
            name="ck_sl_environment_profiles_scope_competition",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Stable identity.
    scope_key: str = Field(
        index=True,
        nullable=False,
        description="Stable key: 'season:<year>' or 'competition:<competition_id>'",
    )
    scope_kind: str = Field(
        nullable=False,
        description="'season_all_competitions' or 'competition'",
    )
    year: int = Field(nullable=False)
    competition_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_competitions.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    venue_slug: Optional[str] = Field(default=None)
    display_name: str = Field(nullable=False)

    # Versioning / selection.
    version: int = Field(
        nullable=False, description="Monotonic version within a scope_key"
    )
    is_current: bool = Field(
        default=False,
        nullable=False,
        description="Marks the single active profile for this scope",
    )
    registry_version: str = Field(
        nullable=False,
        description="Metric-registry definition version this profile was built under",
    )

    # Identity & coverage (game grain).
    included_competitions: int = Field(default=1, nullable=False)
    final_games: int = Field(default=0, nullable=False)
    scheduled_games: int = Field(default=0, nullable=False)
    distinct_teams: int = Field(default=0, nullable=False)
    box_complete_games: int = Field(default=0, nullable=False)
    shot_covered_games: int = Field(default=0, nullable=False)
    pbp_covered_games: int = Field(
        default=0,
        nullable=False,
        description="Informational only in v1; gates no displayed metric",
    )
    games_with_score: int = Field(default=0, nullable=False)
    games_with_known_ot: int = Field(default=0, nullable=False)

    # --- Typed environment metric values (NULL when coverage != complete) ---
    points_per_team_game: Optional[float] = Field(default=None)
    estimated_possessions: Optional[float] = Field(default=None)
    pace_per_48: Optional[float] = Field(default=None)
    offensive_rating: Optional[float] = Field(default=None)
    three_attempt_share: Optional[float] = Field(default=None)
    three_fg_pct: Optional[float] = Field(default=None)
    free_throw_rate: Optional[float] = Field(default=None)
    offensive_rebound_rate: Optional[float] = Field(default=None)
    turnover_rate: Optional[float] = Field(default=None)
    assisted_fg_rate: Optional[float] = Field(default=None)
    rim_attempt_share: Optional[float] = Field(default=None)
    rim_fg_pct: Optional[float] = Field(default=None)
    average_score_margin: Optional[float] = Field(default=None)
    close_game_share: Optional[float] = Field(default=None)
    overtime_share: Optional[float] = Field(default=None)

    # --- Typed performance-landscape metric values ---
    team_ortg_iqr: Optional[float] = Field(default=None)
    top_decile_minutes_share: Optional[float] = Field(default=None)
    top_decile_points_share: Optional[float] = Field(default=None)

    # --- Field-composition scalars (counts of resolved appeared players) ---
    appeared_players: int = Field(
        default=0,
        nullable=False,
        description="Distinct canonical players with positive minutes in a final game",
    )
    appeared_unresolved: int = Field(
        default=0,
        nullable=False,
        description="Distinct unresolved source-player appearances (never collapsed)",
    )
    participation_count: Optional[int] = Field(
        default=None, description="Roster/participation entries when available"
    )
    player_games: int = Field(default=0, nullable=False)
    rookie_count: int = Field(default=0, nullable=False)
    returner_count: int = Field(default=0, nullable=False)
    drafted_count: int = Field(default=0, nullable=False)
    undrafted_count: int = Field(default=0, nullable=False)
    first_round_count: int = Field(default=0, nullable=False)
    second_round_count: int = Field(default=0, nullable=False)
    lottery_count: int = Field(default=0, nullable=False)
    teams_represented: int = Field(default=0, nullable=False)
    median_age: Optional[float] = Field(
        default=None, description="Median age in years at competition start"
    )

    # Freshness / provenance summary.
    calculated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    source_watermark: Optional[datetime] = Field(
        default=None,
        description="Max source-row updated_at pooled across contributing spokes",
    )
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueEnvironmentSeasonMembership(SQLModel, table=True):  # type: ignore[call-arg]
    """A competition pooled into a season (all-competitions) profile.

    Present for ``season_all_competitions`` profiles only; it lists which
    competition editions were summed. The unique constraint prevents a
    competition from being double-counted in one season profile.
    """

    __tablename__ = "summer_league_environment_season_memberships"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "competition_id",
            name="uq_sl_environment_membership_profile_competition",
        ),
        Index(
            "ix_sl_environment_membership_competition",
            "competition_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_environment_profiles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    competition_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_competitions.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    year: int = Field(nullable=False)
    venue_slug: Optional[str] = Field(default=None)
    final_games: int = Field(default=0, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueEnvironmentMetricCoverage(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-metric coverage verdict and reason for a profile.

    ``coverage`` is ``complete`` (every eligible final game carries the input),
    ``partial`` (some do — value stays NULL, never zero), or ``unavailable``
    (none do). This is the honest disclosure layer beside the typed metric
    value columns on the profile.
    """

    __tablename__ = "summer_league_environment_metric_coverage"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "metric_key",
            name="uq_sl_environment_metric_coverage_profile_metric",
        ),
        Index(
            "ix_sl_environment_metric_coverage_profile",
            "profile_id",
        ),
        CheckConstraint(
            "coverage IN ('complete', 'partial', 'unavailable')",
            name="ck_sl_environment_metric_coverage_verdict",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_environment_profiles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    metric_key: str = Field(nullable=False)
    coverage: str = Field(nullable=False)
    covered_games: int = Field(default=0, nullable=False)
    eligible_games: int = Field(default=0, nullable=False)
    reason: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueEnvironmentFieldComposition(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-attribute known/unknown/total counts over resolved appeared players.

    One row per (profile, attribute) where attribute is ``draft`` / ``age`` /
    ``position`` / ``origin``. ``unknown`` resolved players are disclosed
    explicitly rather than collapsed into a denominator, and an optional
    ``distribution`` histogram (e.g. draft-class buckets, position groups)
    supports the drilldown without inflating the profile's typed columns.
    """

    __tablename__ = "summer_league_environment_field_composition"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "attribute_key",
            name="uq_sl_environment_field_composition_profile_attribute",
        ),
        Index(
            "ix_sl_environment_field_composition_profile",
            "profile_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_environment_profiles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    attribute_key: str = Field(
        nullable=False, description="'draft' | 'age' | 'position' | 'origin'"
    )
    known: int = Field(default=0, nullable=False)
    unknown: int = Field(default=0, nullable=False)
    total: int = Field(default=0, nullable=False)
    distribution: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Bucket -> count histogram for this attribute",
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueEnvironmentProvenance(SQLModel, table=True):  # type: ignore[call-arg]
    """Per-source watermark and freshness detail feeding a profile.

    One row per contributing spoke source (``box`` / ``shot`` / ``pbp`` /
    ``score`` / ``ot_state`` / ``identity``) recording the max source-row
    timestamp, contributing row count, and a parse-status summary so the
    Explorer can disclose exactly how fresh and how complete each input was.
    """

    __tablename__ = "summer_league_environment_provenance"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "source_kind",
            name="uq_sl_environment_provenance_profile_source",
        ),
        Index(
            "ix_sl_environment_provenance_profile",
            "profile_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_environment_profiles.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    source_kind: str = Field(
        nullable=False,
        description="'box' | 'shot' | 'pbp' | 'score' | 'ot_state' | 'identity'",
    )
    watermark_at: Optional[datetime] = Field(
        default=None, description="Max updated_at of the contributing source rows"
    )
    row_count: int = Field(default=0, nullable=False)
    parse_status: Optional[str] = Field(
        default=None, description="Optional parse/coverage summary for the source"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

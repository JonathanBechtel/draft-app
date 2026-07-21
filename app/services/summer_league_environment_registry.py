"""Shared Competition Context metric/field registry (frozen v1 definitions).

This module is the single source of truth for *what* each Summer League
environment, performance-landscape, and field-composition metric means. It is
consumed by aggregation (#617), the Explorer read contract (#607), and the
Competitions tab (#608) so a metric is never defined twice. It computes no
production aggregates itself; it declares formulas, denominators, units,
rounding, coverage requirements, sort/filter eligibility, scope eligibility,
and interpretation.

Frozen v1 rules honored here (mirroring the implementation contract §4/§5 and
the Phase-0 coverage audit):

* A season profile **pools raw numerators and denominators** before calculating
  rates; it never averages competition-level rates.
* Each metric is independently nullable. Coverage is ``complete`` / ``partial``
  / ``unavailable`` per metric; ``partial`` is never coerced to zero and never
  certifies a metric.
* **Play-by-play coverage is informational in v1** — no displayed metric is
  gated by it (:data:`CoverageSource.PBP` is intentionally absent from every v1
  metric's ``coverage_source``; :func:`metric_gated_by_pbp` always returns
  ``False``).
* **Assisted-FG** is derived from box-score ``AST / FGM`` — the event-level
  answer to "how assisted was the offense?" — not the player-role ``AST%``.
* **Event-time field composition** (draft / age / position / origin):
    - *Draft status* is evaluated at the competition's event time: a player is
      "drafted" only if drafted on or before the competition year (so a 2026
      rookie counts as drafted in 2026 Summer League).
    - *Age* is computed as of the competition's start date, not today.
    - *Position* is sourced **event-time first**:
      ``summer_league_participation.roster_position`` /
      ``summer_league_player_game_logs.starter_position``, falling back to the
      canonical ``player_status.position_id`` taxonomy only when the event-time
      value is null. (Event-time coverage is ~24%; the canonical fallback keeps
      the attribute publishable while preserving event-time preference.)
    - *Origin* is the pre-event college/international affiliation, not the
      player's current ``players_master`` country/school.
  Unresolved identities stay explicit and never enter a metric denominator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.config import settings


class MetricSection(str, Enum):
    """Which Explorer section a metric belongs to."""

    ENVIRONMENT = "environment"
    LANDSCAPE = "landscape"
    COMPOSITION = "composition"


class MetricUnit(str, Enum):
    """Canonical stored unit for a metric value."""

    RATIO = "ratio"  # stored as a 0-1 fraction
    POINTS = "points"  # points on the scoreboard
    POSSESSIONS = "possessions"  # estimated possessions
    PACE = "pace"  # possessions per 48 minutes
    RATING = "rating"  # per-100-possession points
    YEARS = "years"  # age in years
    COUNT = "count"  # a whole count


class CoverageSource(str, Enum):
    """Input whose per-game coverage certifies a metric across a scope."""

    BOX = "box"  # two complete team-box rows per final game
    SHOT = "shot"  # parsed shot-chart input per final game
    SCORE = "score"  # a known home/away final score per game
    OT_STATE = "ot_state"  # a known overtime state (status_text present)
    PBP = "pbp"  # parsed play-by-play (informational only in v1)
    IDENTITY = "identity"  # resolved appeared-player identity + attribute


class ScopeEligibility(str, Enum):
    """Which scopes a metric may be published for."""

    BOTH = "both"
    SEASON_ONLY = "season_all_competitions"
    COMPETITION_ONLY = "competition"


# Definition version stamped onto every profile built under this registry.
# Bump when any formula/denominator/rounding/coverage rule changes.
REGISTRY_VERSION = "2026.07.2"

# Aggregation/calculation-algorithm version stamped onto every profile,
# distinct from REGISTRY_VERSION (metric definitions/formulas/coverage rules,
# above) and distinct from a profile's own `version` (a per-scope monotonic
# publication sequence number, bumped every rebuild regardless of whether
# anything actually changed). Bump CALCULATION_VERSION when the aggregation
# *pipeline logic* changes -- e.g. which raw inputs are pooled, how the input
# watermark is assembled, possession/coverage wiring -- even when no metric
# formula in this registry changed. A profile carrying an older
# calculation_version than the current constant was built under different
# aggregation logic and is a candidate for rebuild even if its registry_version
# still matches.
CALCULATION_VERSION = "2026.07.5"

# A literal threshold value for tests that want to assert boundary behavior
# at a known number (see is_profile_stale's stale_after_hours override).
# Runtime staleness reads settings.summer_league_environment_stale_after_hours
# instead -- see is_profile_stale below.
PROFILE_STALE_AFTER_HOURS = 72


def is_profile_stale(
    calculated_at: Optional[datetime],
    *,
    stale_after_hours: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Whether a profile's last computation exceeds the freshness threshold.

    Single source of truth for the public "stale badge" behavior (contract
    §8) -- called by the Explorer detail panel, cross-subject context strips,
    and the season/venue summary module so they can never disagree on
    whether a given profile reads as stale. A profile beyond the threshold
    stays the last good, readable version; this only flags it for display,
    it never triggers a request-time recompute or replacement.

    Args:
        calculated_at: The profile's stored computation timestamp, or
            ``None`` for a profile that has never published (never stale).
        stale_after_hours: Explicit threshold override (tests only);
            defaults to the live
            ``settings.summer_league_environment_stale_after_hours``, so an
            operator-configured threshold actually reaches every public
            surface rather than a hardcoded value only the refresh
            pipeline's own tripwire sees.
        now: Reference instant; defaults to the real current time (UTC).

    Returns:
        ``True`` once ``now - calculated_at`` exceeds the threshold.
    """
    if calculated_at is None:
        return False
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    calculated = calculated_at
    if calculated.tzinfo is None:
        calculated = calculated.replace(tzinfo=timezone.utc)
    threshold_hours = (
        stale_after_hours
        if stale_after_hours is not None
        else settings.summer_league_environment_stale_after_hours
    )
    return (reference - calculated).total_seconds() > threshold_hours * 3600


# Field-composition attributes with per-attribute known/unknown coverage, in
# display order. ``draft_class``/``age_reference``/``position_source``/
# ``appearance`` are disclosure dimensions distinct from their sibling base
# attribute: ``age_reference``/``position_source`` disclose *which source*
# resolved the value (known = the preferred event-time source; unknown = a
# disclosed fallback was used), never whether the base attribute itself is
# known.
FIELD_COMPOSITION_ATTRIBUTES: tuple[str, ...] = (
    "draft",
    "draft_class",
    "age",
    "age_reference",
    "position",
    "position_source",
    "appearance",
    "origin",
)


@dataclass(frozen=True)
class MetricDefinition:
    """One certified v1 metric and everything needed to publish it honestly.

    Attributes:
        key: Machine key; also the profile column name when ``stored`` is True.
        label: Human display label.
        section: Explorer grouping (environment / landscape / composition).
        source_fields: Raw input fields (box columns, shot zones) or, for
            derived composition metrics, the stored profile count columns the
            value is computed from.
        formula: Exact pooled formula (numerators/denominators pooled first).
        denominator: The denominator, with its zero-guard note.
        unit: Canonical stored unit.
        scale: Display multiplier (100 turns a 0-1 ratio into a percent).
        rounding: Decimal places for display.
        coverage_source: Input whose ``complete`` coverage certifies the metric.
        sortable: Whether Explorer may sort by it.
        filterable: Whether Explorer may threshold-filter by it (implies a typed
            column).
        scope_eligibility: Which scopes may publish it.
        definition_version: Registry version this definition was frozen under.
        interpretation: One-line reading of what the number means.
        stored: Whether the value is persisted as a typed profile column
            (True) or derived on read from stored count columns (False).
        confidence_note: Optional honesty caveat (e.g. OT inferred from text).
    """

    key: str
    label: str
    section: MetricSection
    source_fields: tuple[str, ...]
    formula: str
    denominator: str
    unit: MetricUnit
    scale: float
    rounding: int
    coverage_source: CoverageSource
    sortable: bool
    filterable: bool
    scope_eligibility: ScopeEligibility
    interpretation: str
    stored: bool = True
    definition_version: str = REGISTRY_VERSION
    confidence_note: Optional[str] = None

    @property
    def gated_by_pbp(self) -> bool:
        """Whether this metric depends on play-by-play (never true in v1)."""
        return self.coverage_source is CoverageSource.PBP


_R = ScopeEligibility.BOTH

# --- Environment metrics (how the basketball played) -----------------------
_ENVIRONMENT: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="points_per_team_game",
        label="Points / Team Game",
        section=MetricSection.ENVIRONMENT,
        source_fields=("pts",),
        formula="sum(team_pts) / team_game_count",
        denominator="team games (2 per box-complete final game); 0 -> None",
        unit=MetricUnit.POINTS,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Average points a team scored per game in this environment.",
    ),
    MetricDefinition(
        key="estimated_possessions",
        label="Possessions / Team Game",
        section=MetricSection.ENVIRONMENT,
        source_fields=("fga", "fgm", "oreb", "dreb", "tov", "fta"),
        # Pooled opponent-adjusted Box.poss at team-game grain, never a competing
        # simple estimate (contract §4). Box.poss is the shared BBRef possession
        # formula reused from app.services.summer_league.metrics.
        formula="sum(Box.poss(team, opponent)) / team_game_count",
        denominator="team games; 0 -> None",
        unit=MetricUnit.POSSESSIONS,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Estimated possessions used per team game.",
    ),
    MetricDefinition(
        key="pace_per_48",
        label="Pace (per 48)",
        section=MetricSection.ENVIRONMENT,
        source_fields=("fga", "oreb", "tov", "fta", "minutes"),
        formula="48 * sum(possessions) / (sum(team_minutes) / 5)",
        denominator="team minutes / 5 (team-minute-fifths); 0 -> None",
        unit=MetricUnit.PACE,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Possessions a team would use over 48 minutes (SL pace is per-48).",
    ),
    MetricDefinition(
        key="offensive_rating",
        label="Offensive Rating",
        section=MetricSection.ENVIRONMENT,
        source_fields=("pts", "fga", "oreb", "tov", "fta"),
        formula="100 * sum(pts) / sum(possessions)",
        denominator="estimated possessions; 0 -> None",
        unit=MetricUnit.RATING,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Points scored per 100 possessions across the environment.",
    ),
    MetricDefinition(
        key="three_attempt_share",
        label="3PA Share",
        section=MetricSection.ENVIRONMENT,
        source_fields=("fg3a", "fga"),
        formula="sum(fg3a) / sum(fga)",
        denominator="field-goal attempts; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of shot attempts taken from three.",
    ),
    MetricDefinition(
        key="three_fg_pct",
        label="3P%",
        section=MetricSection.ENVIRONMENT,
        source_fields=("fg3m", "fg3a"),
        formula="sum(fg3m) / sum(fg3a)",
        denominator="three-point attempts; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Three-point accuracy across the environment.",
    ),
    MetricDefinition(
        key="free_throw_rate",
        label="Free-Throw Rate",
        section=MetricSection.ENVIRONMENT,
        source_fields=("fta", "fga"),
        formula="sum(fta) / sum(fga)",
        denominator="field-goal attempts; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Free-throw attempts drawn per field-goal attempt.",
    ),
    MetricDefinition(
        key="offensive_rebound_rate",
        label="Offensive Rebound Rate",
        section=MetricSection.ENVIRONMENT,
        source_fields=("oreb", "dreb"),
        formula="sum(oreb) / (sum(oreb) + sum(dreb))",
        denominator="all rebounds (pooled OREB + DREB); 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of missed shots recovered by the offense.",
    ),
    MetricDefinition(
        key="turnover_rate",
        label="Turnover Rate",
        section=MetricSection.ENVIRONMENT,
        source_fields=("tov", "fga", "fta"),
        # Frozen contract formula (§4): the plays-based TOV% estimate, not the
        # opponent-adjusted Box.poss possession estimate used for pace/ORtg.
        # Deliberately its own denominator so it never moves when the
        # possession formula is recalibrated.
        formula="sum(tov) / (sum(fga) + 0.44 * sum(fta) + sum(tov))",
        denominator="field-goal attempts + 0.44 * free-throw attempts + "
        "turnovers; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of team plays (FGA + 0.44*FTA + TOV) ending in a turnover.",
    ),
    MetricDefinition(
        key="assisted_fg_rate",
        label="Assisted-FG Rate",
        section=MetricSection.ENVIRONMENT,
        source_fields=("ast", "fgm"),
        formula="sum(ast) / sum(fgm)",
        denominator="made field goals; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of made field goals that were assisted (AST/FGM), "
        "not player AST%.",
    ),
    MetricDefinition(
        key="rim_attempt_share",
        label="Rim Attempt Share",
        section=MetricSection.ENVIRONMENT,
        source_fields=("rim_fga", "shot_fga"),
        formula="sum(rim_fga) / sum(shot_fga)",
        denominator="shot-chart field-goal attempts (backcourt excluded); 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.SHOT,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of shots taken at the rim (Restricted Area).",
    ),
    MetricDefinition(
        key="rim_fg_pct",
        label="Rim FG%",
        section=MetricSection.ENVIRONMENT,
        source_fields=("rim_fgm", "rim_fga"),
        formula="sum(rim_fgm) / sum(rim_fga)",
        denominator="rim field-goal attempts; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.SHOT,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Finishing accuracy at the rim.",
    ),
    MetricDefinition(
        key="average_score_margin",
        label="Avg Score Margin",
        section=MetricSection.ENVIRONMENT,
        source_fields=("home_score", "away_score"),
        formula="sum(abs(home_score - away_score)) / games_with_score",
        denominator="final games with a known score; 0 -> None",
        unit=MetricUnit.POINTS,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.SCORE,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Average final-margin size; lower means tighter games.",
    ),
    MetricDefinition(
        key="close_game_share",
        label="Close-Game Share",
        section=MetricSection.ENVIRONMENT,
        source_fields=("home_score", "away_score"),
        formula="count(abs(margin) <= 5) / games_with_score",
        denominator="final games with a known score; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.SCORE,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of games decided by 5 points or fewer.",
    ),
    MetricDefinition(
        key="overtime_share",
        label="Overtime Share",
        section=MetricSection.ENVIRONMENT,
        source_fields=("status_text",),
        formula="count(status_text ILIKE '%OT%') / games_with_known_ot",
        denominator="final games with a known OT state; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.OT_STATE,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of games that reached overtime.",
        confidence_note="OT inferred from raw status_text, not a normalized flag; "
        "populated only for 2026+.",
    ),
)

# --- Performance-landscape metrics -----------------------------------------
_LANDSCAPE: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="team_ortg_iqr",
        label="Team ORtg Spread (IQR)",
        section=MetricSection.LANDSCAPE,
        source_fields=("pts", "fga", "oreb", "tov", "fta"),
        formula="Q3(team_offensive_rating) - Q1(team_offensive_rating)",
        denominator="box-complete team-game offensive ratings; <4 games -> None",
        unit=MetricUnit.RATING,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Interquartile spread of team offensive ratings; "
        "how varied the offenses were.",
    ),
    MetricDefinition(
        key="top_decile_minutes_share",
        label="Top-Decile Minutes Share",
        section=MetricSection.LANDSCAPE,
        source_fields=("minutes",),
        formula="sum(minutes of top-10% players by minutes) / sum(all minutes)",
        denominator="total appeared-player minutes; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Minutes concentration among the busiest players.",
    ),
    MetricDefinition(
        key="top_decile_points_share",
        label="Top-Decile Points Share",
        section=MetricSection.LANDSCAPE,
        source_fields=("pts",),
        formula="sum(points of top-10% players by points) / sum(all points)",
        denominator="total appeared-player points; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.BOX,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Scoring concentration among the top producers.",
    ),
)

# --- Field-composition metrics ---------------------------------------------
# Shares are derived on read from stored count columns (stored=False); median
# age is persisted because a median cannot be recomputed from counts.
_COMPOSITION: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="rookie_share",
        label="Rookie Share",
        section=MetricSection.COMPOSITION,
        source_fields=("rookie_count", "appeared_players"),
        formula="rookie_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field in its first Summer League appearance.",
        stored=False,
    ),
    MetricDefinition(
        key="returner_share",
        label="Returner Share",
        section=MetricSection.COMPOSITION,
        source_fields=("returner_count", "appeared_players"),
        formula="returner_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field returning from a prior Summer League.",
        stored=False,
    ),
    MetricDefinition(
        key="drafted_share",
        label="Drafted Share",
        section=MetricSection.COMPOSITION,
        source_fields=("drafted_count", "appeared_players"),
        formula="drafted_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field drafted at or before event time.",
        stored=False,
    ),
    MetricDefinition(
        key="undrafted_share",
        label="Undrafted Share",
        section=MetricSection.COMPOSITION,
        source_fields=("undrafted_count", "appeared_players"),
        formula="undrafted_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field undrafted as of event time.",
        stored=False,
    ),
    MetricDefinition(
        key="not_yet_drafted_share",
        label="Not-Yet-Drafted Share",
        section=MetricSection.COMPOSITION,
        source_fields=("not_yet_drafted_count", "appeared_players"),
        formula="not_yet_drafted_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation=(
            "Share of the field whose draft_year is after event time -- "
            "distinct from undrafted (contract §5: 'not yet drafted', never "
            "retrospectively undrafted)."
        ),
        stored=False,
    ),
    MetricDefinition(
        key="first_round_share",
        label="First-Round Share",
        section=MetricSection.COMPOSITION,
        source_fields=("first_round_count", "appeared_players"),
        formula="first_round_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field drafted in the first round.",
        stored=False,
    ),
    MetricDefinition(
        key="second_round_share",
        label="Second-Round Share",
        section=MetricSection.COMPOSITION,
        source_fields=("second_round_count", "appeared_players"),
        formula="second_round_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field drafted in the second round.",
        stored=False,
    ),
    MetricDefinition(
        key="lottery_share",
        label="Lottery Share",
        section=MetricSection.COMPOSITION,
        source_fields=("lottery_count", "appeared_players"),
        formula="lottery_count / appeared_players",
        denominator="distinct resolved appeared players; 0 -> None",
        unit=MetricUnit.RATIO,
        scale=100.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Share of the field drafted in the lottery (round 1, pick <= 14).",
        stored=False,
    ),
    MetricDefinition(
        key="median_age",
        label="Median Age",
        section=MetricSection.COMPOSITION,
        source_fields=("median_age",),
        formula="median(age_at_competition_start of resolved appeared players)",
        denominator="resolved appeared players with a known birthdate; 0 -> None",
        unit=MetricUnit.YEARS,
        scale=1.0,
        rounding=1,
        coverage_source=CoverageSource.IDENTITY,
        sortable=True,
        filterable=True,
        scope_eligibility=_R,
        interpretation="Median event-time age of the field.",
        stored=True,
    ),
)


METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    _ENVIRONMENT + _LANDSCAPE + _COMPOSITION
)

METRICS_BY_KEY: dict[str, MetricDefinition] = {d.key: d for d in METRIC_DEFINITIONS}


# ---------------------------------------------------------------------------
# Registry helpers.
# ---------------------------------------------------------------------------


def get_metric(key: str) -> MetricDefinition:
    """Return the definition for ``key`` or raise ``KeyError``."""
    return METRICS_BY_KEY[key]


def all_metric_keys() -> tuple[str, ...]:
    """Every v1 metric key in registry order."""
    return tuple(d.key for d in METRIC_DEFINITIONS)


def metrics_in_section(section: MetricSection) -> tuple[MetricDefinition, ...]:
    """All metric definitions in a section, in registry order."""
    return tuple(d for d in METRIC_DEFINITIONS if d.section is section)


def stored_metric_keys() -> tuple[str, ...]:
    """Keys of metrics persisted as a typed profile column."""
    return tuple(d.key for d in METRIC_DEFINITIONS if d.stored)


def filterable_metric_keys() -> tuple[str, ...]:
    """Keys eligible for Explorer threshold filtering."""
    return tuple(d.key for d in METRIC_DEFINITIONS if d.filterable)


def sortable_metric_keys() -> tuple[str, ...]:
    """Keys eligible for Explorer sorting."""
    return tuple(d.key for d in METRIC_DEFINITIONS if d.sortable)


def metrics_for_scope(scope_kind: str) -> tuple[MetricDefinition, ...]:
    """Metrics publishable for a scope kind, honoring scope eligibility."""
    return tuple(
        d
        for d in METRIC_DEFINITIONS
        if d.scope_eligibility is ScopeEligibility.BOTH
        or d.scope_eligibility.value == scope_kind
    )


def metric_gated_by_pbp(key: str) -> bool:
    """Whether a metric is gated by play-by-play coverage (always False in v1).

    Play-by-play is informational only; no displayed v1 metric depends on it.
    """
    return get_metric(key).gated_by_pbp


def safe_ratio(
    numerator: Optional[float], denominator: Optional[float]
) -> Optional[float]:
    """Divide with a zero/None-denominator guard.

    Returns ``None`` (never zero, never a raised error) when the denominator is
    missing or zero, so an undefined rate is disclosed as unknown rather than
    fabricated as 0.

    Args:
        numerator: The numerator; ``None`` yields ``None``.
        denominator: The denominator; ``None`` or ``0`` yields ``None``.

    Returns:
        The quotient, or ``None`` when it is undefined.
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def format_metric_value(key: str, value: Optional[float]) -> str:
    """Format a stored metric value for display per its unit/scale/rounding.

    Args:
        key: Metric key.
        value: The stored (canonical) value, or ``None`` when uncovered.

    Returns:
        A display string. ``None`` renders as an em dash; ratios render as a
        percentage with a trailing ``%``.
    """
    if value is None:
        return "—"
    definition = get_metric(key)
    scaled = round(value * definition.scale, definition.rounding)
    text = f"{scaled:.{definition.rounding}f}"
    if definition.unit is MetricUnit.RATIO:
        return f"{text}%"
    return text


@dataclass(frozen=True)
class RegistrySummary:
    """Small structured summary of the frozen registry (for diagnostics)."""

    version: str
    metric_count: int
    stored_metric_count: int
    section_counts: dict[str, int] = field(default_factory=dict)


def registry_summary() -> RegistrySummary:
    """Return a structured summary of the current registry."""
    section_counts: dict[str, int] = {}
    for definition in METRIC_DEFINITIONS:
        section_counts[definition.section.value] = (
            section_counts.get(definition.section.value, 0) + 1
        )
    return RegistrySummary(
        version=REGISTRY_VERSION,
        metric_count=len(METRIC_DEFINITIONS),
        stored_metric_count=len(stored_metric_keys()),
        section_counts=section_counts,
    )

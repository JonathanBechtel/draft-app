"""The shared stat-engine metric registry — declare each metric exactly once.

This module is the spine Phase 2 (`docs/plans/summer-league-stat-engine-reuse-spec.md`
§2, ticket T7 / issue #724) exists to build: a declarative catalog of every box-derived
metric the engine in :mod:`app.services.stats.formulas` computes, so no metric's formula,
rollup semantics, or comparison guardrails are ever declared twice. It computes no
aggregates itself and imports nothing from ``app.services.stats.formulas`` beyond the
shape of the engine it documents -- see the module docstring in
``app/services/stats/__init__.py`` for that engine's own contract.

**Shape precedent.** This mirrors ``app.services.summer_league_environment_registry`` --
a frozen dataclass per metric carrying formula text, denominator, unit, and
interpretation, collected into a keyed lookup with small accessor helpers. That module
cannot be imported from here (import-linter contract 3 forbids
``app.services.stats -> app.services.summer_league_environment_registry``), so this is a
sibling with the same idiom, not a shared base class.

**``rollup_class`` is the highest-value field.** How a metric aggregates across grains
(game -> competition -> career) has been re-derived by hand at least five times across
the Explorer, the leaders board, and the Class Tracker, and has already produced two
bugs -- the SQL-sort ``COALESCE`` gotcha and the ws82/vorp82 reclassification. Three
values, closed set:

* ``recombinable`` -- recompute the formula from summed box components at the target
  grain (e.g. sum FGA/FTA/PTS across games, then divide once). Never average
  pre-computed per-grain values.
* ``additive_share`` -- the metric is itself a summable share (Win Shares, VORP): sum
  the raw per-grain values, then re-share into a rate (WS/48, WS/82) if one is needed.
* ``pool_recalibrated`` -- the value depends on pool-relative context (league VOP/DRB%,
  the pace-standardization scalar, a pool-fit BPM regression, or -- per the pace
  grain-mismatch bug in #732 -- a possession estimate that must never be reused at a
  different grain than it was computed for). Must be recomputed against the pool
  context at the grain in question; never averaged, never carried across grains.

**``METRIC_REGISTRY_VERSION`` is the canonical source of the existing constant** --
see the version-layering note below the constant's definition.

**The frozen ``turnover_rate`` decision.**
``app/services/summer_league_environment_service.py`` computes
``FGA + 0.44*FTA + TOV`` under an explicit "Frozen contract formula (§4)" comment,
deliberately independent of this engine's pooled ``tov_pct``/possession-based turnover
rate -- it must never move when the possession formula is recalibrated. This registry
does **not** silently repoint that site at the engine's ``tov_pct``. Instead it declares
the frozen formula as its own entry, :data:`ENVIRONMENT_TURNOVER_RATE_FROZEN`, carrying
a distinct ``metric_key`` (``environment_turnover_rate``) and its own
``definition_version`` pinned to the Competition Context registry's frozen v1 contract,
with ``is_frozen_exemption=True`` and an ``exemption_reason`` recording why. This is the
comment-discipline exemption `docs/plans/programmatic-code-discipline.md` §1.3 requires,
expressed as a registry entry rather than a bare code comment so both formulas --
engine ``tov_pct`` and frozen environment ``turnover_rate`` -- are declared exactly
once each and are distinguishable by key. See the package report for the full reasoning
and for why a later agent's confinement-rule allowlist (T9 / #730) should treat
``summer_league_environment_service.py``'s literal ``0.44`` as this declared, justified
exemption rather than an undeclared duplicate.
"""

# discipline: file-size declarative one-entry-per-metric catalog (T7 #724); per-metric adjacency of Python and SQL forms is the design, splitting recreates the drift T6 removed

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class MetricFamily(str, Enum):
    """Comparison-semantics family, per the Player Development Ledger taxonomy.

    Source: ``docs/plans/player-longitudinal-evidence-layer-pitch.md`` -- "What is
    comparable -- and what is only contextual". The same registry that de-dupes the
    math also carries the guardrails that stop, e.g., a Summer League PER being
    charted against an NBA PER as though the labels alone made them comparable.
    """

    SHARED_RATE = "shared_rate"
    ROLE_DEPENDENT_VOLUME = "role_dependent_volume"
    COMPETITION_RELATIVE_COMPOSITE = "competition_relative_composite"


class MetricUnit(str, Enum):
    """Canonical unit a metric's raw (unscaled) value is expressed in."""

    RATIO = "ratio"  # stored as a 0-1 fraction unless noted otherwise
    PERCENT = "percent"  # stored as a 0-100 value
    POINTS = "points"  # box-score points, or a points-scale composite (Game Score)
    RATING = "rating"  # per-100-possession points (ORtg/DRtg/Net)
    PACE = "pace"  # possessions per 48 minutes
    WINS = "wins"  # Win Shares scale
    COUNT = "count"  # a whole count


class RollupClass(str, Enum):
    """How a metric aggregates across grains (game -> competition -> career).

    See the module docstring for the full definition of each value and the two bugs
    this taxonomy has already caused when re-derived by hand instead of read from one
    place.
    """

    RECOMBINABLE = "recombinable"
    ADDITIVE_SHARE = "additive_share"
    POOL_RECALIBRATED = "pool_recalibrated"


class Grain(str, Enum):
    """A scope a metric may validly be reported at."""

    GAME = "game"
    COMPETITION = "competition"
    CAREER = "career"


class ReferenceKind(str, Enum):
    """What a metric's value may be benchmarked against.

    Taken from the Ledger pitch's comparison-semantics guardrail: matching field
    names alone do not make two values comparable.
    """

    SAME_COMPETITION_POOL = "same_competition_pool"
    SAME_DRAFT_COHORT = "same_draft_cohort"
    SAME_POSITION_GROUP = "same_position_group"
    CROSS_SOURCE_SAME_FORMULA = "cross_source_same_formula"
    NONE = "none"


# ---------------------------------------------------------------------------
# Version.
# ---------------------------------------------------------------------------
#
# These are the canonical values. The materialized Summer League schema imports them
# rather than declaring a second pair of literals. Keeping the calculation stamp tied
# to the registry stamp means a formula-registry bump propagates to newly published
# rows with one edit; the names remain separate in the row shape so a future pipeline
# change can split them deliberately.
#
# Bump this (and update the assertion test) whenever any formula/rollup/comparison
# field on a non-exempt entry below changes; bump each affected entry's own
# ``definition_version`` in the same change.
METRIC_REGISTRY_VERSION = "2026.07.1"
METRIC_CALCULATION_VERSION = METRIC_REGISTRY_VERSION


@dataclass(frozen=True)
class MetricDefinition:
    """One declared metric: its formula, rollup semantics, and comparison guardrails.

    Attributes:
        metric_key: Machine key. Distinct from any stored column name by design --
            a metric can be declared here before (or without) ever being persisted.
        metric_family: Comparison-semantics family (see :class:`MetricFamily`).
        unit: Canonical unit of the raw (unscaled) value.
        denominator: The denominator in words, with its zero-guard note.
        definition_version: The registry version this exact formula/rollup/semantics
            declaration was frozen under. Equal to :data:`METRIC_REGISTRY_VERSION`
            for every entry except the frozen exemption, which pins its own.
        requires: Canonical input fields the formula needs, by name. Box fields use
            :class:`~app.services.stats.inputs.StatInputs`' field names (``fga``,
            ``fta``, ``tov``, ...); ``"team_box"`` / ``"opponent_box"`` denote a need
            for the teammate/opponent box (not just the player's own); a PBP-derived
            metric lists its PBP input by name (e.g. ``"ast_fgm"``, ``"unast_fgm"``)
            rather than the box's plain ``"ast"`` -- this is what the T8 capability
            model (#728) derives availability from, since a source providing box-only
            inputs does not provide PBP-derived fields.
        formula: The exact formula, in the same "numerators/denominators pooled
            first" notation as the environment registry precedent.
        rollup_class: How the metric aggregates across grains (see
            :class:`RollupClass`).
        grain_validity: Which grains the engine can validly compute this metric at
            today.
        comparison_semantics: One-line rule for when this value may be compared to
            another value carrying the same metric_key, per the Ledger taxonomy.
        allowed_reference_kinds: What the value may be benchmarked against.
        minimum_sample_rule: The minimum sample below which the value should not be
            published/compared.
        coverage_requirement: The source coverage needed to certify this metric
            (box-only, or box + play-by-play).
        interpretation_note: One-line reading of what the number means.
        is_frozen_exemption: True for a metric deliberately kept independent of a
            newer/consolidated formula under this same registry (see the module
            docstring's frozen ``turnover_rate`` note). False for every ordinary
            entry.
        exemption_reason: Required (non-``None``) when ``is_frozen_exemption`` is
            True; the justifying comment discipline requires.
    """

    metric_key: str
    metric_family: MetricFamily
    unit: MetricUnit
    denominator: str
    definition_version: str
    requires: tuple[str, ...]
    formula: str
    rollup_class: RollupClass
    grain_validity: tuple[Grain, ...]
    comparison_semantics: str
    allowed_reference_kinds: tuple[ReferenceKind, ...]
    minimum_sample_rule: str
    coverage_requirement: str
    interpretation_note: str
    is_frozen_exemption: bool = False
    exemption_reason: Optional[str] = None


_GAME_AND_COMPETITION = (Grain.GAME, Grain.COMPETITION)
_GAME_COMPETITION_AND_CAREER = (Grain.GAME, Grain.COMPETITION, Grain.CAREER)
_COMPETITION_ONLY = (Grain.COMPETITION,)
_COMPETITION_AND_CAREER = (Grain.COMPETITION, Grain.CAREER)
_POOL_REF = (ReferenceKind.SAME_COMPETITION_POOL,)
_POOL_AND_COHORT_REF = (
    ReferenceKind.SAME_COMPETITION_POOL,
    ReferenceKind.SAME_DRAFT_COHORT,
)
_CROSS_SOURCE_REF = (
    ReferenceKind.SAME_COMPETITION_POOL,
    ReferenceKind.CROSS_SOURCE_SAME_FORMULA,
)
_SHARED_RATE_SEMANTICS = (
    "Directly comparable to another value with this metric_key only when both were "
    "computed from this exact formula and denominator; pair with event/league "
    'context and sample size (Ledger pitch §"shared-rate metric").'
)
_ROLE_VOLUME_SEMANTICS = (
    "Do not present as a raw per-game delta across role/usage contexts; compare via "
    "this per-minute/per-possession view alongside minutes, usage, and starts "
    '(Ledger pitch §"role-dependent volume").'
)
_COMPOSITE_SEMANTICS = (
    "Meaningful only within its native competition/pool; never chart against a "
    "different level's or source's same-named metric without a validated "
    'translation study (Ledger pitch §"competition-relative composite").'
)
_BOX_COVERAGE = "box: two complete box lines (player + team) per counted game"
_BOX_OPP_COVERAGE = (
    "box: complete player, team, and opponent-team box lines per counted game"
)
_BOX_POOL_COVERAGE = (
    "box: complete player/team/opponent box lines pooled to an adv_eligible "
    "competition pool (league VOP/DRB%/pace context available)"
)
_PBP_COVERAGE = (
    "pbp: parsed play-by-play assist attribution (ast_fgm/unast_fgm) per counted "
    "game, in addition to box"
)
_ADV_ELIGIBLE_SAMPLE = (
    "Pool must be adv_eligible (league-context thresholds met); individual value "
    "is 0->None guarded on its own denominator."
)


# --- Shooting / participation rates (recombinable) --------------------------
_SHARED_RATES: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_key="ts_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="2 * (field-goal attempts + 0.44 * free-throw attempts); 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("pts", "fga", "fta"),
        formula="100 * PTS / (2 * (FGA + 0.44 * FTA))",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_CROSS_SOURCE_REF,
        minimum_sample_rule="fga + 0.44*fta > 0",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note="Scoring efficiency accounting for threes and free throws.",
    ),
    MetricDefinition(
        metric_key="efg_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="field-goal attempts; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("fgm", "fg3m", "fga"),
        formula="100 * (FGM + 0.5 * FG3M) / FGA",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_CROSS_SOURCE_REF,
        minimum_sample_rule="fga > 0",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note="Field-goal efficiency crediting the extra value of a three.",
    ),
    MetricDefinition(
        metric_key="fg3ar",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.RATIO,
        denominator="field-goal attempts; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("fg3a", "fga"),
        formula="FG3A / FGA",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_CROSS_SOURCE_REF,
        minimum_sample_rule="fga > 0",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note="Share of field-goal attempts taken from three (3-Point Attempt Rate).",
    ),
    MetricDefinition(
        metric_key="ftr",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.RATIO,
        denominator="field-goal attempts; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("fta", "fga"),
        formula="FTA / FGA",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_CROSS_SOURCE_REF,
        minimum_sample_rule="fga > 0",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note=(
            "Free-throw attempts drawn per field-goal attempt (Free-Throw Rate). "
            "The engine's pooled value -- distinct from the frozen environment "
            "turnover_rate's own FGA+0.44*FTA+TOV denominator below, which never "
            "uses this metric."
        ),
    ),
    MetricDefinition(
        metric_key="tov_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator=(
            "field-goal attempts + 0.44 * free-throw attempts + turnovers; 0 -> 0.0"
        ),
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("tov", "fga", "fta"),
        formula="100 * TOV / (FGA + 0.44 * FTA + TOV)",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_CROSS_SOURCE_REF,
        minimum_sample_rule="fga + 0.44*fta + tov > 0",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note=(
            "Share of a player's plays ending in a turnover. Same formula shape as "
            "the frozen environment turnover_rate below, but this is the "
            "player-grain engine value -- the two are declared separately and "
            "neither repoints the other; see the module docstring."
        ),
    ),
    MetricDefinition(
        metric_key="usg_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="minutes-weighted team plays; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("fga", "fta", "tov", "mp", "team_box"),
        formula=(
            "100 * ((FGA + 0.44*FTA + TOV) * (TmMP/5)) / (MP * (TmFGA + 0.44*TmFTA "
            "+ TmTOV))"
        ),
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team_box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note=(
            "Share of team plays used by this player while on the floor. NBA "
            "Advanced-feed value is authoritative when present; this is the "
            "box-computed fallback."
        ),
    ),
    MetricDefinition(
        metric_key="ast_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="teammate field goals made while on floor; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("ast", "mp", "team_box"),
        formula="100 * AST / ((MP / (TmMP/5)) * TmFGM - FGM)",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team_box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note=(
            "Share of teammate field goals a player assisted while on the floor. "
            "NBA Advanced-feed value is authoritative when present; this is the "
            "box-computed fallback."
        ),
    ),
    MetricDefinition(
        metric_key="astd_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="a player's own made field goals; 0 -> None",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("ast_fgm", "unast_fgm"),
        formula="100 * ast_fgm / (ast_fgm + unast_fgm)",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="ast_fgm + unast_fgm > 0",
        coverage_requirement=_PBP_COVERAGE,
        interpretation_note=(
            "Share of a player's own made field goals that were assisted, from "
            "parsed play-by-play assist attribution -- distinct from the "
            "environment registry's assisted_fg_rate (AST/FGM, an event-level "
            "'how assisted was the offense' box-only metric, not this player-role "
            "PBP-derived one)."
        ),
    ),
    MetricDefinition(
        metric_key="orb_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="minutes-weighted team+opponent rebounding chances; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("oreb", "mp", "team_box", "opponent_box"),
        formula="100 * OREB * (TmMP/5) / (MP * (TmOREB + OppDREB))",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team+opponent box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note="Share of available offensive rebounds grabbed while on the floor.",
    ),
    MetricDefinition(
        metric_key="drb_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="minutes-weighted team+opponent rebounding chances; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("dreb", "mp", "team_box", "opponent_box"),
        formula="100 * DREB * (TmMP/5) / (MP * (TmDREB + OppOREB))",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team+opponent box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note="Share of available defensive rebounds grabbed while on the floor.",
    ),
    MetricDefinition(
        metric_key="trb_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="minutes-weighted team+opponent rebounding chances; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("reb", "mp", "team_box", "opponent_box"),
        formula="100 * REB * (TmMP/5) / (MP * (TmREB + OppREB))",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team+opponent box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note=(
            "Share of all available rebounds grabbed while on the floor. NBA "
            "Advanced-feed value is authoritative when present; this is the "
            "box-computed fallback."
        ),
    ),
    MetricDefinition(
        metric_key="stl_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="minutes-weighted opponent possessions; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("stl", "mp", "team_box", "opponent_box"),
        formula="100 * STL * (TmMP/5) / (MP * OppPoss)",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team+opponent box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note="Share of opponent possessions ending in a steal while on the floor.",
    ),
    MetricDefinition(
        metric_key="blk_pct",
        metric_family=MetricFamily.SHARED_RATE,
        unit=MetricUnit.PERCENT,
        denominator="minutes-weighted opponent 2-point attempts; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("blk", "mp", "team_box", "opponent_box"),
        formula="100 * BLK * (TmMP/5) / (MP * (OppFGA - OppFG3A))",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_SHARED_RATE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 and team+opponent box present",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note="Share of opponent 2-point attempts blocked while on the floor.",
    ),
    MetricDefinition(
        metric_key="gmsc",
        metric_family=MetricFamily.ROLE_DEPENDENT_VOLUME,
        unit=MetricUnit.POINTS,
        denominator="games played (season grain); N/A at game grain",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=(
            "pts",
            "fgm",
            "fga",
            "ftm",
            "fta",
            "oreb",
            "dreb",
            "stl",
            "ast",
            "blk",
            "pf",
            "tov",
        ),
        formula=(
            "PTS + 0.4*FGM - 0.7*FGA - 0.4*(FTA-FTM) + 0.7*OREB + 0.3*DREB + STL "
            "+ 0.7*AST + 0.7*BLK - 0.4*PF - TOV"
        ),
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_ROLE_VOLUME_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0 (any box line)",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note="Hollinger Game Score: one-number per-game production, no league constants.",
    ),
)


# ---------------------------------------------------------------------------
# SQL push-down forms (T6, #727).
# ---------------------------------------------------------------------------
#
# Doc #2 §4's fallback, not a formula-to-SQL compiler: the metrics the Explorer
# pushes into SQL (``ts_pct``, ``tov_pct`` above) get their SQL form declared
# here, next to the ``MetricDefinition`` it must agree with, bound to the
# Python form (``app.services.stats.formulas.ts_pct_ratio`` /
# ``tov_pct_ratio``) by ``tests/unit/services/stats/test_sql_python_parity.py``
# (structural — does the SQL text/expression the function emits look right)
# and ``tests/integration/test_stat_engine_parity.py`` (behavioral — does it
# evaluate to the same number as the Python form, against a real DB).
#
# ``box`` is a column-naming callable: pass a callable that wraps a field
# name for the aggregate (career) grain -- ``SUM(...)`` in text,
# ``func.sum(...)`` in an expression -- or leaves it bare for row grain
# (per-competition / per-game), and one declaration emits both SQL shapes.
# Each metric gets two such declarations, not four notations: one ``*_expr``
# form (``box`` returns a SQLAlchemy expression, for the SQLAlchemy-expression
# call sites) and one ``*_sql_text`` form (``box`` returns a string, for the
# raw-SQL-text ``ORDER BY`` call sites) -- the two notations the Explorer
# already uses for push-down, both required to agree with the Python form.


def ts_pct_denom_expr(box: Callable[[str], Any]) -> Any:
    """ts_pct's SQLAlchemy denominator expression: ``2 * (FGA + 0.44 * FTA)``.

    Matches :func:`app.services.stats.formulas.ts_pct_ratio`'s denominator
    exactly. ``box`` maps a field name to its SQLAlchemy expression at the
    target grain -- ``func.sum(getattr(table, name))`` for the career/
    aggregate grain, the bare ``getattr(table, name)`` for per-competition/
    per-game row grain -- so this one declaration emits both shapes.
    """
    return 2.0 * (box("fga") + 0.44 * box("fta"))


def tov_pct_denom_expr(box: Callable[[str], Any]) -> Any:
    """tov_pct's SQLAlchemy denominator expression: ``FGA + 0.44*FTA + TOV``.

    Matches :func:`app.services.stats.formulas.tov_pct_ratio`'s denominator
    exactly. The Explorer only pushes ``tov_pct`` down in SQL at row grain
    (per-game); ``box`` still takes the aggregate-capable shape so a future
    aggregate-grain call site can reuse this same declaration.
    """
    return box("fga") + 0.44 * box("fta") + box("tov")


def ts_pct_sql_text(box: Callable[[str], str]) -> str:
    """ts_pct's raw-SQL-text form: ``PTS / NULLIF(2 * (FGA + 0.44*FTA), 0)``.

    Matches :func:`app.services.stats.formulas.ts_pct_ratio` exactly. ``box``
    wraps a column label in ``SUM(...)`` for the aggregate grain or leaves it
    bare for row grain -- the same indirection :func:`game_score_sql_text`
    uses for Game Score.
    """
    return f"{box('pts')} / NULLIF(2.0 * ({box('fga')} + 0.44 * {box('fta')}), 0)"


def tov_pct_sql_text(box: Callable[[str], str]) -> str:
    """tov_pct's raw-SQL-text form: ``TOV*100 / NULLIF(FGA+0.44*FTA+TOV, 0)``.

    Matches :func:`app.services.stats.formulas.tov_pct_ratio` exactly (scaled
    by 100 for the percent display, as the Python form's ``round(..., 1)``
    twin does).
    """
    return (
        f"{box('tov')} * 100.0 / "
        f"NULLIF({box('fga')} + 0.44 * {box('fta')} + {box('tov')}, 0)"
    )


def astd_pct_denom_expr(box: Callable[[str], Any]) -> Any:
    """astd_pct's SQLAlchemy denominator expression: ``ast_fgm + unast_fgm``.

    Matches :func:`app.services.stats.formulas.astd_pct_ratio`'s denominator
    exactly. ``box`` maps a field name to its SQLAlchemy expression at the
    target grain -- ``func.sum(getattr(table, name))`` for the career/
    aggregate grain, the bare ``getattr(table, name)`` for per-competition/
    per-game row grain -- the same indirection :func:`ts_pct_denom_expr` uses.
    """
    return box("ast_fgm") + box("unast_fgm")


def astd_pct_sql_text(box: Callable[[str], str]) -> str:
    """astd_pct's raw-SQL-text sort form: ``ast_fgm / NULLIF(ast_fgm+unast_fgm, 0)``.

    Unscaled (``* 1.0`` for float division, no ``* 100``) because its call
    sites (the Explorer's ``ORDER BY`` sort expressions) only need the value
    for sorting -- sort order is invariant to a constant multiplier -- the
    same convention :func:`ts_pct_sql_text` uses.
    """
    return f"{box('ast_fgm')} * 1.0 / NULLIF({box('ast_fgm')} + {box('unast_fgm')}, 0)"


def efg_pct_num_expr(box: Callable[[str], Any]) -> Any:
    """efg_pct's SQLAlchemy numerator expression: ``FGM + 0.5 * FG3M``.

    The dual of :func:`ts_pct_denom_expr`: eFG%'s only coefficient lives in its
    numerator (the half-credit for a three), not its denominator, so this is the
    part a call site must not retype. Matches
    :func:`app.services.stats.formulas.efg_pct_ratio` and
    :func:`efg_pct_sql_text` exactly. ``box`` maps a field name to its
    SQLAlchemy expression at the target grain -- ``func.sum(getattr(table,
    name))`` for the career/aggregate grain, the bare ``getattr(table, name)``
    for per-competition/per-game row grain -- so this one declaration emits
    both shapes.

    Added by the Phase 2 QA gate (#731). T6 bound the Explorer's *raw-SQL-text*
    eFG% forms to :func:`efg_pct_sql_text` but left its three *SQLAlchemy
    expression* filter sites hand-written, so changing the half-credit weight in
    this package moved the displayed value while the filter kept selecting rows
    by the old weight -- with no test and no guard firing. There is no matching
    ``fg3ar``/``ftr`` expression helper on purpose: those two are a bare column
    over a bare column with no coefficient, so their expression form has nothing
    that can numerically drift from their declared ``*_sql_text`` form.
    """
    return box("fgm") + 0.5 * box("fg3m")


def efg_pct_sql_text(box: Callable[[str], str]) -> str:
    """efg_pct's raw-SQL-text sort form: ``(FGM + 0.5*FG3M) / NULLIF(FGA, 0)``.

    Matches :func:`app.services.stats.formulas.efg_pct_ratio` exactly, unscaled
    (no ``* 100``) because its call sites (the Explorer's ``ORDER BY`` sort
    expressions) only need the value for sorting -- sort order is invariant to
    a constant multiplier -- the same convention :func:`astd_pct_sql_text`
    uses. ``box`` wraps a column label in ``SUM(...)`` for the aggregate grain
    or leaves it bare for row grain -- the same indirection
    :func:`ts_pct_sql_text` uses.
    """
    return f"({box('fgm')} + 0.5 * {box('fg3m')}) / NULLIF({box('fga')}, 0)"


def fg3ar_sql_text(box: Callable[[str], str]) -> str:
    """fg3ar's raw-SQL-text sort form: ``FG3A * 1.0 / NULLIF(FGA, 0)``.

    Matches :func:`app.services.stats.formulas.fg3ar_ratio` exactly. ``box``
    wraps a column label in ``SUM(...)`` for the aggregate grain or leaves it
    bare for row grain -- the same indirection :func:`ts_pct_sql_text` uses.
    """
    return f"{box('fg3a')} * 1.0 / NULLIF({box('fga')}, 0)"


def ftr_sql_text(box: Callable[[str], str]) -> str:
    """FTr's raw-SQL-text sort form: ``FTA * 1.0 / NULLIF(FGA, 0)``.

    Matches :func:`app.services.stats.formulas.ftr_ratio` exactly. ``box``
    wraps a column label in ``SUM(...)`` for the aggregate grain or leaves it
    bare for row grain -- the same indirection :func:`ts_pct_sql_text` uses.
    """
    return f"{box('fta')} * 1.0 / NULLIF({box('fga')}, 0)"


def game_score_sql_text(box: Callable[[str], str]) -> str:
    """Hollinger Game Score's raw-SQL-text form (T9, #730).

    Matches :func:`app.services.stats.formulas.game_score` exactly. ``box``
    wraps a column label in ``SUM(...)`` (or a NULL-coalescing variant) for
    the aggregate grain or leaves it bare for row grain -- the same
    indirection :func:`ts_pct_sql_text` uses. Byte-identical to the literal
    formerly built by ``_game_score_sql`` in
    ``app.services.summer_league_explorer_service`` (verified at HEAD before
    T9 folded it in here): the Explorer's ORDER BY sort expression duplicated
    the Game Score weights in a raw f-string outside this package, which is
    exactly the shape the stat-constant confinement checker
    (``scripts/check_stat_constants.py``) exists to catch and which T6 did not
    reach (T6's scope was the nine 0.44 sites; the Game Score weights
    0.4/0.7/0.3 were untouched).
    """
    return (
        f"({box('pts')} + 0.4 * {box('fgm')} - 0.7 * {box('fga')} "
        f"- 0.4 * ({box('fta')} - {box('ftm')}) + 0.7 * {box('oreb')} "
        f"+ 0.3 * {box('dreb')} + {box('stl')} + 0.7 * {box('ast')} "
        f"+ 0.7 * {box('blk')} - 0.4 * {box('pf')} - {box('tov')})"
    )


# --- Scaled counting forms (recombinable) -----------------------------------
# Representative entries for the per-36 / per-100 scaling class T4 (#725)
# consolidates into one definition; the same scaling applies to every counting
# stat the Explorer exposes (pts, reb, ast, stl, blk, tov, ...), not to points
# alone -- these two entries stand for that shared arithmetic.
_SCALED_FORMS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_key="pts_per36",
        metric_family=MetricFamily.ROLE_DEPENDENT_VOLUME,
        unit=MetricUnit.POINTS,
        denominator="minutes played; 0 -> None",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("pts", "mp"),
        formula="PTS * 36 / MP  (sum PTS and MP at the target grain, then scale once)",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_ROLE_VOLUME_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="mp > 0",
        coverage_requirement=_BOX_COVERAGE,
        interpretation_note=(
            "Points scaled to a 36-minute rate. Representative of the shared "
            "per-36 scaling applied to every counting stat; recombine raw totals "
            "at the desired grain before scaling, never rescale an already-scaled "
            "value from a different grain."
        ),
    ),
    MetricDefinition(
        metric_key="pts_per100",
        metric_family=MetricFamily.ROLE_DEPENDENT_VOLUME,
        unit=MetricUnit.RATING,
        denominator="estimated player possessions; pool must clear MIN_POOL_PACE, else None",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("pts", "mp", "team_box", "opponent_box"),
        formula="100 * PTS / (TmPoss(opp) * (MP / (TmMP/5)))",
        rollup_class=RollupClass.RECOMBINABLE,
        grain_validity=_GAME_COMPETITION_AND_CAREER,
        comparison_semantics=_ROLE_VOLUME_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule=(
            "pool pace >= MIN_POOL_PACE (40.0) and team possessions > 0, else the "
            "value is None rather than a fabricated (explosive) rate"
        ),
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note=(
            "Points scaled to a 100-possession rate, from this player's own "
            "team+opponent box (not a pool-wide pace) -- box-derived, not a "
            "league-relative composite, per the engine's own comment in "
            "compute_metrics. Representative of the shared per-100 scaling class. "
            "Recompute fresh at the target grain; see pace below for the "
            "grain-mismatch risk this scaling shares with it (#732)."
        ),
    ),
)

# --- Pool-recalibrated composites -------------------------------------------
_POOL_RECALIBRATED: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_key="pace",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.PACE,
        denominator="team-minute-fifths (TmMP/5); pool must clear MIN_POOL_PACE, else None",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("mp", "team_box", "opponent_box"),
        formula="48 * (TmPoss(opp) + OppPoss(tm)) / (2 * TmMP/5)",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_GAME_AND_COMPETITION,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="pool pace >= MIN_POOL_PACE (40.0) and team possessions > 0",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note=(
            "Team pace assigned to a player's games. Classified pool_recalibrated "
            "(not recombinable) per the parked #732 follow-up: a pace value from "
            "one grain (e.g. a full season) must never be reused as the divisor "
            "for a different grain's (e.g. one game's) counting stats -- it must "
            "always be recomputed fresh, from this same formula, at the grain "
            "actually being displayed."
        ),
    ),
    MetricDefinition(
        metric_key="uper",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.RATING,
        denominator="minutes played, scaled by league VOP/DRB%/ast-factor context; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=(
            "mp",
            "fgm",
            "fga",
            "fg3m",
            "ftm",
            "fta",
            "ast",
            "reb",
            "oreb",
            "stl",
            "blk",
            "tov",
            "pf",
            "team_box",
            "pool_context",
        ),
        formula="Bbref unadjusted PER: (1/MP) * (weighted box terms using VOP/DRB%/factor)",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_COMPETITION_ONLY,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE,
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "Unadjusted PER; the engine additionally pace-standardizes this into "
            "aper by multiplying by (pool pace / this player's team pace) before "
            "publication."
        ),
    ),
    MetricDefinition(
        metric_key="ortg",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.RATING,
        denominator="Dean Oliver individual scoring possessions; 0 -> None",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=(
            "pts",
            "fgm",
            "fga",
            "ftm",
            "fta",
            "ast",
            "oreb",
            "team_box",
            "opponent_box",
        ),
        formula="100 * individual points produced / individual scoring possessions",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_GAME_AND_COMPETITION,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE
        + " Game-grain value needs both team boxes present.",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note=(
            "Individual offensive rating (Dean Oliver). Season-grain publication "
            "is gated by pool adv_eligible alongside DRtg/WS/BPM; the game-grain "
            "form (game_advanced_line) needs only that single game's team+"
            "opponent boxes."
        ),
    ),
    MetricDefinition(
        metric_key="drtg",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.RATING,
        denominator="team defensive possessions; 0 -> None",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("mp", "stl", "blk", "dreb", "pf", "team_box", "opponent_box"),
        formula="team DRtg + 0.2 * (100 * opponent scoring-poss rate * (1 - stop%) - team DRtg)",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_GAME_AND_COMPETITION,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE
        + " Game-grain value needs both team boxes present.",
        coverage_requirement=_BOX_OPP_COVERAGE,
        interpretation_note="Individual defensive rating (Dean Oliver); gated with ortg.",
    ),
    MetricDefinition(
        metric_key="net_rating",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.RATING,
        denominator="N/A -- a difference of two ratings",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("ortg", "drtg"),
        formula="ORtg - DRtg",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_COMPETITION_ONLY,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE,
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note="Individual Net Rating; inherits ortg/drtg's pool gate exactly.",
    ),
    MetricDefinition(
        metric_key="ws",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.WINS,
        denominator="marginal points per win (pool-context scaled); 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=(
            "pts",
            "fgm",
            "fga",
            "ftm",
            "fta",
            "ast",
            "oreb",
            "mp",
            "team_box",
            "opponent_box",
            "pool_context",
        ),
        formula="OWS + DWS, each = marginal points (offense/defense) / marginal points per win",
        rollup_class=RollupClass.ADDITIVE_SHARE,
        grain_validity=_COMPETITION_AND_CAREER,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE,
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "Raw Win Shares (OWS+DWS) are additive across grains -- career totals "
            "sum a player's per-competition WS directly, no recomputation needed. "
            "Distinct from the ws82 rate below, which is not additive; see that "
            "entry."
        ),
    ),
    MetricDefinition(
        metric_key="ws40",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.WINS,
        denominator="minutes played; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("ws", "mp"),
        formula="40 * WS / MP",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_COMPETITION_ONLY,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE,
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "WS re-shared to a per-40-minute rate -- a projection, not the raw "
            "additive WS. Do not average across grains; recompute WS40 from that "
            "grain's own summed WS and minutes."
        ),
    ),
    MetricDefinition(
        metric_key="ws82",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.WINS,
        denominator="team-minute-fifths (TmMP/5), an 82-game/48-min projection factor; 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("ws", "team_box"),
        formula="WS * (48 * 82) / (TmMP/5)",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_COMPETITION_ONLY,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule=_ADV_ELIGIBLE_SAMPLE,
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "**Known-correct classification (previously misclassified as "
            "recombinable -- a real bug).** WS/82 projects raw WS to an 82-game "
            "pace using a competition-specific team-minutes projection factor; "
            "that factor differs per competition/pool, so summing or averaging "
            "WS82 across two competitions does not produce a valid combined value "
            "the way summing raw WS does. Must be recomputed against the target "
            "grain's own pool context, never averaged."
        ),
    ),
    MetricDefinition(
        metric_key="bpm",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.RATING,
        denominator="N/A -- a fitted regression score, pool-mean centered",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=(
            "fgm",
            "fg3m",
            "ftm",
            "fga",
            "fta",
            "oreb",
            "dreb",
            "ast",
            "stl",
            "blk",
            "tov",
            "pf",
            "pool_context",
        ),
        formula="OBPM + DBPM, each = SL-native fitted per-100-poss regression, centered to pool mean",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_COMPETITION_ONLY,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="player_poss > 0 and a fitted pool-wide coefficient vector exists",
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "Box Plus-Minus (OBPM + DBPM); the SL-native fit is refit per pool and "
            "every player's centering is relative to that pool's minute-weighted "
            "mean, so a BPM value only means something within the pool it was "
            "produced in."
        ),
    ),
    MetricDefinition(
        metric_key="vorp",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.WINS,
        denominator="N/A -- points-above-replacement accrued over minutes played",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("bpm", "mp"),
        formula="(BPM - VORP_REPLACEMENT) * MP / (48 * 82)",
        rollup_class=RollupClass.ADDITIVE_SHARE,
        grain_validity=_COMPETITION_AND_CAREER,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_REF,
        minimum_sample_rule="player_poss > 0 and a fitted pool-wide coefficient vector exists",
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "Raw cumulative VORP is additive across competitions (same "
            "convention as raw WS): a career total sums each competition's vorp "
            "directly. Distinct from vorp82 below, which is not additive; see "
            "that entry."
        ),
    ),
    MetricDefinition(
        metric_key="vorp82",
        metric_family=MetricFamily.COMPETITION_RELATIVE_COMPOSITE,
        unit=MetricUnit.WINS,
        denominator="share of available team-lineup minutes (pct_min); 0 -> 0.0",
        definition_version=METRIC_REGISTRY_VERSION,
        requires=("bpm", "mp", "team_box"),
        formula="(BPM - VORP_REPLACEMENT) * (MP / (TmMP/5))",
        rollup_class=RollupClass.POOL_RECALIBRATED,
        grain_validity=_COMPETITION_ONLY,
        comparison_semantics=_COMPOSITE_SEMANTICS,
        allowed_reference_kinds=_POOL_AND_COHORT_REF,
        minimum_sample_rule="player_poss > 0 and a fitted pool-wide coefficient vector exists",
        coverage_requirement=_BOX_POOL_COVERAGE,
        interpretation_note=(
            "**Known-correct classification (previously misclassified as "
            "recombinable -- a real bug).** VORP/82 projects raw VORP to a "
            "full-season pace using this competition's own team-minutes share; "
            "like ws82 it must be recomputed against the target grain's pool "
            "context, never averaged or reused across competitions."
        ),
    ),
)

# --- Frozen exemption --------------------------------------------------------
# See the module docstring's "The frozen turnover_rate decision" section.
ENVIRONMENT_TURNOVER_RATE_FROZEN = MetricDefinition(
    metric_key="environment_turnover_rate",
    metric_family=MetricFamily.SHARED_RATE,
    unit=MetricUnit.PERCENT,
    denominator=(
        "field-goal attempts + 0.44 * free-throw attempts + turnovers, pooled at "
        "the Competition Context environment scope; 0 -> None"
    ),
    # Pinned to the Competition Context registry's own frozen v1 contract version
    # (app.services.summer_league_environment_registry.REGISTRY_VERSION at the time
    # this exemption was declared), not to METRIC_REGISTRY_VERSION -- this formula
    # does not move when the engine's tov_pct/definition_version does.
    definition_version="2026.07.3",
    requires=("tov", "fga", "fta"),
    formula="sum(TOV) / (sum(FGA) + 0.44 * sum(FTA) + sum(TOV))",
    rollup_class=RollupClass.RECOMBINABLE,
    grain_validity=_COMPETITION_ONLY,
    comparison_semantics=_SHARED_RATE_SEMANTICS,
    allowed_reference_kinds=_POOL_REF,
    minimum_sample_rule="pooled fga + 0.44*fta + tov > 0",
    coverage_requirement=_BOX_COVERAGE,
    interpretation_note=(
        "Environment-scope turnover rate: share of team plays (FGA + 0.44*FTA + "
        "TOV) ending in a turnover, pooled across an entire competition/season "
        "environment -- not a player value. Deliberately independent of this "
        "registry's player-grain tov_pct: 'Frozen contract formula (§4)' in "
        "app/services/summer_league_environment_service.py, so it never moves "
        "when the pooled possession estimate behind tov_pct is recalibrated. Do "
        "not repoint app.services.summer_league_environment_service at tov_pct; "
        "the two are intentionally allowed to diverge."
    ),
    is_frozen_exemption=True,
    exemption_reason=(
        "app/services/summer_league_environment_service.py:1715 computes this "
        "exact formula under an explicit 'Frozen contract formula (§4)' "
        "comment, deliberately independent of the pooled engine's possession "
        "estimate so the Competition Context environment metrics never silently "
        "move when app.services.stats recalibrates tov_pct/possessions. Declared "
        "here (T7 / #724) as its own registry entry with its own metric_key and "
        "definition_version, rather than silently repointed at the engine's "
        "tov_pct, per docs/plans/programmatic-code-discipline.md §1.3. T9's "
        "(#730) stat-constant confinement allowlist should treat this site's "
        "literal 0.44 as this declared, justified exemption."
    ),
)


METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    _SHARED_RATES
    + _SCALED_FORMS
    + _POOL_RECALIBRATED
    + (ENVIRONMENT_TURNOVER_RATE_FROZEN,)
)

METRICS_BY_KEY: dict[str, MetricDefinition] = {
    d.metric_key: d for d in METRIC_DEFINITIONS
}


# ---------------------------------------------------------------------------
# Registry helpers.
# ---------------------------------------------------------------------------


def get_metric(key: str) -> MetricDefinition:
    """Return the definition for ``key`` or raise ``KeyError``."""
    return METRICS_BY_KEY[key]


def all_metric_keys() -> tuple[str, ...]:
    """Every declared metric key, in registry order."""
    return tuple(d.metric_key for d in METRIC_DEFINITIONS)


def metrics_by_rollup_class(rollup_class: RollupClass) -> tuple[MetricDefinition, ...]:
    """All metric definitions carrying a given :class:`RollupClass`."""
    return tuple(d for d in METRIC_DEFINITIONS if d.rollup_class is rollup_class)


def metrics_by_family(family: MetricFamily) -> tuple[MetricDefinition, ...]:
    """All metric definitions in a given :class:`MetricFamily`."""
    return tuple(d for d in METRIC_DEFINITIONS if d.metric_family is family)


def frozen_exemptions() -> tuple[MetricDefinition, ...]:
    """Every metric declared as a frozen exemption rather than a live formula."""
    return tuple(d for d in METRIC_DEFINITIONS if d.is_frozen_exemption)


def requires_for(key: str) -> tuple[str, ...]:
    """Canonical input fields ``key`` needs -- what T8's capability model reads."""
    return get_metric(key).requires


def rollup_class_matches(key: str, *expected: RollupClass) -> bool:
    """True when ``key`` is declared in the registry under one of ``expected``.

    This is the live read T8b (#729) exists to introduce at the five sites that
    used to hand-derive the recombinable/additive_share/pool_recalibrated
    taxonomy: a small `assert`/gate against this function instead of a local
    comment or tuple asserting the same fact.

    Returns ``False`` for a key with **no** registry entry as well as for one
    declared under a *different* class -- the two cases are deliberately not
    distinguished here (callers that need to tell "undeclared" from "declared
    differently" apart should use :data:`METRICS_BY_KEY` directly). Most
    metric keys these call sites handle -- ``fg_pct``/``fg3_pct``/``ft_pct``,
    ``per``/``obpm``/``dbpm`` -- are not yet in the registry at all (T7 scoped
    it to the metrics T4/T5/T6 actually consolidated), so an undeclared key is
    the common, unremarkable case, not a defect.
    """
    entry = METRICS_BY_KEY.get(key)
    return entry is not None and entry.rollup_class in expected


def require_rollup_class(site: str, expected: RollupClass, *keys: str) -> None:
    """Import-time adoption guard: raise unless every key is declared ``expected``.

    The T8b (#729) adoption sites originally cross-checked their hand-written
    key tuples with bare module-level ``assert rollup_class_matches(...)``
    statements. ``python -O`` / ``PYTHONOPTIMIZE`` strips ``assert`` entirely,
    which would dissolve the adoption without a trace, so those sites call this
    instead: the same import-time failure on a reclassified or removed registry
    entry, but one the interpreter cannot optimize away.

    Args:
        site: Short human-readable name of the calling site, used in the error.
        expected: The rollup class every key must be declared under.
        *keys: Registry metric keys to check.

    Raises:
        LookupError: If any key is missing from the registry or declared under
            a different ``rollup_class``.
    """
    for key in keys:
        if not rollup_class_matches(key, expected):
            raise LookupError(
                f"{site}: {key!r} must be declared {expected.value!r} in "
                "app.services.stats.registry (entry missing or reclassified)"
            )


@dataclass(frozen=True)
class RegistrySummary:
    """Small structured summary of the current registry (for diagnostics)."""

    version: str
    metric_count: int
    rollup_class_counts: dict[str, int]


def registry_summary() -> RegistrySummary:
    """Return a structured summary of the current registry."""
    rollup_counts: dict[str, int] = {}
    for definition in METRIC_DEFINITIONS:
        rollup_counts[definition.rollup_class.value] = (
            rollup_counts.get(definition.rollup_class.value, 0) + 1
        )
    return RegistrySummary(
        version=METRIC_REGISTRY_VERSION,
        metric_count=len(METRIC_DEFINITIONS),
        rollup_class_counts=rollup_counts,
    )


def net_rating_expr(box: Callable[[str], Any]) -> Any:
    """Build the neutral SQLAlchemy form of ``ORtg - DRtg``."""
    return box("off_rating") - box("def_rating")


def pace_per_48_expr(box: Callable[[str], Any]) -> Any:
    """Build pooled possessions per 48 team minutes."""
    return 48.0 * box("possessions") / (box("team_minutes") / 5.0)


def points_per_100_expr(box: Callable[[str], Any]) -> Any:
    """Build the neutral SQLAlchemy form of points per 100 possessions."""
    return 100.0 * box("points") / box("possessions")

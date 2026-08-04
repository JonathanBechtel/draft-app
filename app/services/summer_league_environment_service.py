"""Scope boundaries and deterministic aggregation for Competition Context.

This module defines the stable :class:`EnvironmentScope` value object and the
current-profile lookup consumed by the Explorer read contract (#607) and
cross-surface reuse (#609/#610), **and** (#617) the deterministic, set-based
aggregation that materializes versioned season and competition profiles from the
normalized Summer League spokes.

Aggregation honors the frozen implementation contract
(``docs/plans/competition-context-explorer-implementation-contract.md``):

* Only ``status == FINAL`` games contribute basketball / appeared-player facts
  (§3). Scheduled / in-progress / postponed / canceled / unknown games are
  counted for schedule disclosure only.
* Every rate recomputes from **pooled numerators and denominators**; a season
  profile pools every normalized competition in its year (§2/§4). Possessions
  reuse the opponent-adjusted :class:`app.services.summer_league.metrics.Box`
  possession estimate — never a competing formula (§4).
* Each metric is independently certified ``complete`` / ``partial`` /
  ``unavailable`` from audited input coverage; a partial metric is published as
  ``NULL`` plus counts + reason, never coerced to zero (§3). Play-by-play is
  informational and gates no displayed metric.
* Certification reads audited input/file status, never the mere existence of
  one fact row (§3; hardened by #635): a box-complete game needs exactly two
  team-box rows with **every** metric-required field non-null
  (:func:`_box_row_usable` -- a null field silently becomes 0 in
  ``Box.add_row`` otherwise); a shot/PBP-complete game needs its
  ``SummerLeagueRawFile.parse_status`` to be ``PARSED`` for that specific game
  (:func:`_load_game_parse_status`), and an unmapped/unknown non-backcourt shot
  zone additionally uncertifies that game's shot coverage.
* Publication acquires the transaction-scoped Summer League writer lock
  **before** the first source read and holds the same transaction through
  calculation, validation, version insertion, and the atomic ``is_current``
  switch (§8). A failed candidate leaves the prior current profile readable.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.schemas.players_master import PlayerMaster
from app.schemas.player_status import PlayerStatus
from app.schemas.positions import Position
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeagueShotEvent,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_environment import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    SCOPE_KIND_COMPETITION,
    SCOPE_KIND_SEASON,
    SummerLeagueEnvironmentFieldComposition,
    SummerLeagueEnvironmentMetricCoverage,
    SummerLeagueEnvironmentProfile,
    SummerLeagueEnvironmentProvenance,
    SummerLeagueEnvironmentSeasonMembership,
)
from app.schemas.summer_league import (
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRun,
    SummerLeagueRawRunStatus,
)
from app.services.stats.percentiles import percentile as _percentile
from app.services.summer_league.metrics import MIN_COMPLETE_TEAM_MP, Box

# discipline: file-size formula audit keeps this legacy aggregation module reviewable.
from app.services.stats.formulas import pace_per_48, points_per_100
from app.services.summer_league.write_lock import acquire_summer_league_writer_lock
from app.services.summer_league_environment_registry import (
    CALCULATION_VERSION,
    FIELD_COMPOSITION_ATTRIBUTES,
    REGISTRY_VERSION,
    CoverageSource,
    MetricDefinition,
    MetricSection,
    METRIC_DEFINITIONS,
    format_metric_value,
    get_metric,
    is_profile_stale,
    safe_ratio,
)

ScopeKind = Literal["season_all_competitions", "competition"]

# A game decided by this margin or fewer is a "close game" (registry §4).
CLOSE_GAME_MARGIN = 5
# NBA SHOT_ZONE_BASIC label for at-the-rim attempts, and the excluded backcourt
# zone (heaves carry no scouting signal; same exclusion as the shot services).
RIM_ZONE = "Restricted Area"
BACKCOURT_ZONE = "Backcourt"
# The full NBA SHOT_ZONE_BASIC taxonomy this pipeline understands -- mirrors
# app.services.summer_league.metrics._ZONE_TO_BUCKET plus its excluded
# Backcourt zone. A shot row whose zone is null or outside this set is
# "unmapped": a parse/taxonomy gap, not a legitimate backcourt heave, and it
# invalidates that game's shot certification rather than silently dropping
# out of the denominator (contract §3).
_KNOWN_SHOT_ZONES: frozenset[str] = frozenset(
    {
        RIM_ZONE,
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Left Corner 3",
        "Right Corner 3",
        "Above the Break 3",
        BACKCOURT_ZONE,
    }
)
# Minimum team-game sample needed to report an IQR-based landscape metric
# (registry): both team_ortg_iqr and team_points_iqr gate on this same floor
# since each box-complete team-game contributes exactly one entry to both
# pooled lists.
MIN_LANDSCAPE_SAMPLE = 4
# Fallback reference month/day when an event date is absent (contract §5 age).
FALLBACK_MONTH_DAY = (7, 1)

# Team-box fields every BOX-sourced metric formula reads (registry
# source_fields union: points_per_team_game/estimated_possessions/pace/
# offensive_rating/three_attempt_share/three_fg_pct/free_throw_rate/
# offensive_rebound_rate/turnover_rate/assisted_fg_rate/team_ortg_iqr all
# resolve to some subset of these). A team-box row is "usable" only when
# every one of these is non-null -- ``Box.add_row`` silently treats a null
# field as 0 (``getattr(r, f) or 0``), so a game with an incomplete row must
# never reach that pooling step uncaught (contract §3: "exactly two usable
# team-box rows with all fields required by the metric").
_REQUIRED_BOX_FIELDS: tuple[str, ...] = (
    "minutes",
    "pts",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "fta",
    "oreb",
    "dreb",
    "tov",
    "ast",
)


def _box_row_usable(row: Any) -> bool:
    """Whether one team-box row clears the minute floor and field-completeness bar.

    Both conditions are required for the row to count toward a box-complete
    game (contract §3): the regulation-minute floor rules out a garbage/
    reserves-only line, and every metric-required field being non-null rules
    out a partially-parsed row silently zero-filling into the pooled totals.
    """
    minutes = row.minutes or 0
    if minutes < MIN_COMPLETE_TEAM_MP:
        return False
    return all(getattr(row, f) is not None for f in _REQUIRED_BOX_FIELDS)


# The service returns the persisted ORM row as the profile boundary type. The
# alias keeps call sites reading against the contract's ``EnvironmentProfile``
# name without coupling to the table class spelling.
EnvironmentProfile = SummerLeagueEnvironmentProfile


@dataclass(frozen=True)
class EnvironmentScope:
    """A stable Competition Context scope identity.

    Attributes:
        scope_kind: ``season_all_competitions`` or ``competition``.
        year: The competition calendar year.
        competition_id: The canonical competition id for a competition scope;
            ``None`` for a season (all-competitions) scope.
        scope_key: The stable key ``season:<year>`` or
            ``competition:<competition_id>`` — never a display label.
    """

    scope_kind: ScopeKind
    year: int
    competition_id: Optional[int]
    scope_key: str

    @classmethod
    def for_season(cls, year: int) -> "EnvironmentScope":
        """Build the all-competitions season scope for a year."""
        return cls(
            scope_kind="season_all_competitions",
            year=year,
            competition_id=None,
            scope_key=season_scope_key(year),
        )

    @classmethod
    def for_competition(cls, competition_id: int, year: int) -> "EnvironmentScope":
        """Build the scope for one named competition edition."""
        return cls(
            scope_kind="competition",
            year=year,
            competition_id=competition_id,
            scope_key=competition_scope_key(competition_id),
        )


def season_scope_key(year: int) -> str:
    """Stable season scope key ``season:<year>``."""
    return f"season:{year}"


def competition_scope_key(competition_id: int) -> str:
    """Stable competition scope key ``competition:<competition_id>``."""
    return f"competition:{competition_id}"


async def get_current_profile_by_scope_key(
    db: AsyncSession, scope_key: str
) -> Optional[EnvironmentProfile]:
    """Return the single *current* profile for a raw stable ``scope_key``.

    Explicitly selects the one row flagged ``is_current`` for the scope key; a
    partial unique index guarantees at most one such row exists, so no ordering
    tie-break is needed. Returns ``None`` when no current profile is published
    yet (readers should then present a stale/empty state, never fabricate one).

    Unlike :func:`get_environment_profile`, this does not require building an
    :class:`EnvironmentScope` (which needs a year) — useful for the Explorer's
    ``competition_id``-only detail lookups (#607), where the year is not known
    until the row is resolved.

    Args:
        db: Async session.
        scope_key: The stable ``season:<year>`` / ``competition:<competition_id>``
            key to resolve.

    Returns:
        The current :class:`SummerLeagueEnvironmentProfile`, or ``None``.
    """
    result = await db.execute(
        select(SummerLeagueEnvironmentProfile).where(
            col(SummerLeagueEnvironmentProfile.scope_key) == scope_key,
            col(SummerLeagueEnvironmentProfile.is_current).is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_environment_profile(
    db: AsyncSession, scope: EnvironmentScope
) -> Optional[EnvironmentProfile]:
    """Return the single *current* profile for a scope, or ``None``.

    Args:
        db: Async session.
        scope: The stable scope identity to resolve.

    Returns:
        The current :class:`SummerLeagueEnvironmentProfile`, or ``None``.
    """
    return await get_current_profile_by_scope_key(db, scope.scope_key)


async def list_current_profiles(
    db: AsyncSession,
    *,
    scope_kind: ScopeKind,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
    venue_slug: Optional[str] = None,
) -> list[EnvironmentProfile]:
    """Every *current* profile for one scope kind, optionally year/venue-scoped.

    One indexed query (``ix_sl_environment_profiles_kind_year`` plus the
    partial ``is_current`` unique index) regardless of how many years/
    competitions match — never a per-scope loop (contract §9). A season scope
    never carries a venue (an all-competitions season pools every venue in the
    year); ``venue_slug`` is only honored for ``scope_kind="competition"``.

    Args:
        db: Async session.
        scope_kind: ``"season_all_competitions"`` or ``"competition"``.
        year_min: Optional inclusive lower year bound.
        year_max: Optional inclusive upper year bound.
        venue_slug: Optional venue filter (competition scope only).

    Returns:
        Current profiles matching the scope, ordered by year ascending.
    """
    stored_kind = (
        SCOPE_KIND_COMPETITION if scope_kind == "competition" else SCOPE_KIND_SEASON
    )
    conds = [
        col(SummerLeagueEnvironmentProfile.scope_kind) == stored_kind,
        col(SummerLeagueEnvironmentProfile.is_current).is_(True),
    ]
    if year_min is not None:
        conds.append(col(SummerLeagueEnvironmentProfile.year) >= year_min)
    if year_max is not None:
        conds.append(col(SummerLeagueEnvironmentProfile.year) <= year_max)
    if venue_slug is not None and stored_kind == SCOPE_KIND_COMPETITION:
        conds.append(col(SummerLeagueEnvironmentProfile.venue_slug) == venue_slug)
    result = await db.execute(
        select(SummerLeagueEnvironmentProfile)
        .where(*conds)
        .order_by(col(SummerLeagueEnvironmentProfile.year))
    )
    return list(result.scalars())


async def list_season_membership(
    db: AsyncSession, profile_id: int
) -> list[SummerLeagueEnvironmentSeasonMembership]:
    """Every competition pooled into one season (all-competitions) profile.

    Args:
        db: Async session.
        profile_id: The season profile's primary key.

    Returns:
        Membership rows ordered by venue then year (stable display order).
    """
    result = await db.execute(
        select(SummerLeagueEnvironmentSeasonMembership)
        .where(col(SummerLeagueEnvironmentSeasonMembership.profile_id) == profile_id)
        .order_by(
            col(SummerLeagueEnvironmentSeasonMembership.venue_slug),
            col(SummerLeagueEnvironmentSeasonMembership.year),
        )
    )
    return list(result.scalars())


async def list_field_composition(
    db: AsyncSession, profile_id: int
) -> list[SummerLeagueEnvironmentFieldComposition]:
    """Per-attribute known/unknown/total counts for one profile (contract §5).

    One indexed query over ``summer_league_environment_field_composition`` —
    called only when a detail profile is selected, never per list row, so it
    stays inside the Explorer query budget (contract §9). Every distribution
    discloses known, unknown, and total so a missing attribute never silently
    leaves a denominator.

    Args:
        db: Async session.
        profile_id: The selected profile's primary key.

    Returns:
        Field-composition rows in registry attribute order
        (draft, age, position, origin).
    """
    result = await db.execute(
        select(SummerLeagueEnvironmentFieldComposition).where(
            col(SummerLeagueEnvironmentFieldComposition.profile_id) == profile_id
        )
    )
    rows = list(result.scalars())
    order = {key: i for i, key in enumerate(FIELD_COMPOSITION_ATTRIBUTES)}
    rows.sort(key=lambda r: order.get(r.attribute_key, len(order)))
    return rows


# Display order for the six provenance source kinds plus the two freshness-
# only kinds added by this ticket (participation/schedule never gate a
# displayed metric -- they exist purely so the input watermark advances).
_PROVENANCE_SOURCE_ORDER: tuple[str, ...] = (
    "box",
    "shot",
    "score",
    "ot_state",
    "pbp",
    "identity",
    "participation",
    "schedule",
)


async def list_provenance(
    db: AsyncSession, profile_id: int
) -> list[SummerLeagueEnvironmentProvenance]:
    """Every per-source provenance row for one profile (exact source references).

    One indexed query over ``summer_league_environment_provenance`` — called
    only when a detail profile is selected, never per list row, mirroring
    :func:`list_field_composition` (contract §9). Every row discloses the
    source's freshness watermark, contributing row count, and (where the
    underlying data models it) parse/source status — the audit trail behind
    a published profile.

    Args:
        db: Async session.
        profile_id: The selected profile's primary key.

    Returns:
        Provenance rows in a stable display order (box/shot/score/ot_state/
        pbp/identity/participation/schedule).
    """
    result = await db.execute(
        select(SummerLeagueEnvironmentProvenance).where(
            col(SummerLeagueEnvironmentProvenance.profile_id) == profile_id
        )
    )
    rows = list(result.scalars())
    order = {key: i for i, key in enumerate(_PROVENANCE_SOURCE_ORDER)}
    rows.sort(key=lambda r: order.get(r.source_kind, len(order)))
    return rows


@dataclass(frozen=True)
class MetricCoverageInfo:
    """A metric's read-time coverage verdict, counts, and reason for one profile."""

    metric_key: str
    coverage: str
    covered: int
    eligible: int
    reason: Optional[str]


def _covered_eligible_for_source(
    profile: EnvironmentProfile, source: CoverageSource
) -> tuple[int, int]:
    """Return ``(covered, eligible)`` game/identity counts for a coverage source.

    Read-time derivation from the profile's own stored disclosure counts —
    mirrors the aggregation-time pairing in :func:`_coverage_for_source` so a
    profile's coverage never needs a separate per-metric query (contract §9).
    """
    final_games = profile.final_games
    if source is CoverageSource.BOX:
        return profile.box_complete_games, final_games
    if source is CoverageSource.SHOT:
        return profile.shot_covered_games, final_games
    if source is CoverageSource.SCORE:
        return profile.games_with_score, final_games
    if source is CoverageSource.OT_STATE:
        return profile.games_with_known_ot, final_games
    if source is CoverageSource.PBP:
        return profile.pbp_covered_games, final_games
    # IDENTITY: an appeared player is "covered" when resolved to a canonical id.
    resolved = profile.appeared_players
    eligible = resolved + profile.appeared_unresolved
    return resolved, eligible


def coverage_for_source(
    profile: EnvironmentProfile, source: CoverageSource
) -> tuple[str, int, int]:
    """Return ``(verdict, covered, eligible)`` for one coverage source.

    Read-time-only: uses the profile's own stored counts, never a query.

    Args:
        profile: A current profile row.
        source: Which input's coverage to evaluate.

    Returns:
        ``(verdict, covered, eligible)`` where ``verdict`` is
        ``"complete"``/``"partial"``/``"unavailable"``.
    """
    covered, eligible = _covered_eligible_for_source(profile, source)
    return _coverage_verdict(covered, eligible), covered, eligible


def metric_coverage_for_profile(
    profile: EnvironmentProfile, definition: MetricDefinition
) -> MetricCoverageInfo:
    """The read-time coverage verdict for one registry metric on one profile.

    Args:
        profile: A current profile row.
        definition: The metric's registry definition.

    Returns:
        A :class:`MetricCoverageInfo` with the verdict, counts, and reason.
    """
    verdict, covered, eligible = coverage_for_source(
        profile, definition.coverage_source
    )
    reason = _coverage_reason(definition.coverage_source, verdict, covered, eligible)
    return MetricCoverageInfo(
        metric_key=definition.key,
        coverage=verdict,
        covered=covered,
        eligible=eligible,
        reason=reason,
    )


# Composition share metrics (stored=False) map to the numerator count column
# they are computed from at read time; the denominator is always
# ``appeared_players``. ``median_age`` is stored=True and is not included here.
_COMPOSITION_SHARE_NUMERATOR_COLUMN: dict[str, str] = {
    "rookie_share": "rookie_count",
    "returner_share": "returner_count",
    "drafted_share": "drafted_count",
    "undrafted_share": "undrafted_count",
    "not_yet_drafted_share": "not_yet_drafted_count",
    "first_round_share": "first_round_count",
    "second_round_share": "second_round_count",
    "lottery_share": "lottery_count",
}


def registry_raw_value(
    profile: EnvironmentProfile, definition: MetricDefinition
) -> Optional[float]:
    """The unscaled canonical value of one registry metric on one profile.

    Stored metrics (``definition.stored``) read their typed profile column
    directly — already ``NULL`` when coverage was not ``complete`` at build
    time for box/shot/score/OT-gated metrics (contract §3). Derived
    composition shares (``stored=False``) are computed on read from the
    profile's persisted count columns; this never re-aggregates raw facts
    (contract §9) and is safe to call for every row in a list response.

    Args:
        profile: A current profile row.
        definition: The metric's registry definition.

    Returns:
        The raw (unscaled, e.g. 0-1 for a ratio) value, or ``None`` when
        undefined.
    """
    if definition.stored:
        value = getattr(profile, definition.key, None)
        return None if value is None else float(value)
    numerator_col = _COMPOSITION_SHARE_NUMERATOR_COLUMN.get(definition.key)
    if numerator_col is None:
        return None
    return safe_ratio(getattr(profile, numerator_col, None), profile.appeared_players)


# ===========================================================================
# #610 — page-ready DTO shaping for the season-hub and venue-page reuse.
#
# Contract §7: "Every consumer receives the same service DTO, values,
# definitions, coverage, and calculation version; routes/templates never
# recompute metrics." This section builds a compact, template-ready summary
# from a single already-fetched current profile row — no additional query —
# using the same shared registry primitives (``registry_raw_value``,
# ``metric_coverage_for_profile``, ``format_metric_value``) the Explorer
# consumes, so the season/venue modules can never diverge from the tab.
# ===========================================================================

# A curated headline subset for the compact season/venue module — the full
# v1 metric/field-composition breakdown remains one click away in Explorer
# (contract §7: the summary "never replaces or masquerades as" the full
# surface). Order matches display order.
_HEADLINE_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "pace_per_48",
    "offensive_rating",
    "three_attempt_share",
    "turnover_rate",
    "assisted_fg_rate",
    "rim_attempt_share",
)
_HEADLINE_COMPOSITION_KEYS: tuple[str, ...] = (
    "rookie_share",
    "drafted_share",
    "lottery_share",
    "median_age",
)


@dataclass(frozen=True)
class PageMetricView:
    """One registry metric resolved for a compact page module.

    Field names deliberately mirror the Explorer detail panel's
    ``MetricValueView`` so both surfaces can share the same
    ``metric_section``/``coverage_badge`` Jinja macros (no divergent markup).
    """

    key: str
    label: str
    formatted_value: str
    unit: str
    formula: str
    denominator: str
    interpretation: str
    confidence_note: Optional[str]
    coverage: str  # "complete" | "partial" | "unavailable"
    covered: int
    eligible: int
    reason: Optional[str]


@dataclass(frozen=True)
class PageMetricSectionView:
    """One labeled group of headline metrics for a page module."""

    key: str
    label: str
    metrics: list[PageMetricView]


@dataclass(frozen=True)
class ProfileSummaryView:
    """A compact, template-ready Competition Context summary for one profile.

    Built once per page render from a single current profile row (contract
    §9: at most one indexed profile read); every value, coverage verdict,
    and version comes from the shared registry/service so it is identical to
    the corresponding Explorer row (contract §7).
    """

    scope_key: str
    scope_kind: str  # "season_all_competitions" | "competition"
    year: int
    competition_id: Optional[int]
    venue_slug: Optional[str]
    display_name: str
    version: int
    registry_version: str
    calculation_version: str
    calculated_at: Optional[datetime]
    source_watermark: Optional[datetime]
    is_stale: bool
    included_competitions: int
    final_games: int
    scheduled_games: int
    distinct_teams: int
    appeared_players: int
    appeared_unresolved: int
    explorer_href: str
    sections: list[PageMetricSectionView]


def explorer_competitions_href(scope: EnvironmentScope) -> str:
    """The canonical Explorer Competitions-tab URL for one scope (contract §6).

    Args:
        scope: The season or competition scope to link to.

    Returns:
        ``/stats/summer-league/explorer?subject=competitions&profile_scope=...``
        with ``detail_year`` (season) or ``competition_id`` (competition) —
        the exact identifiers the contract marks authoritative.
    """
    if scope.scope_kind == "season_all_competitions":
        return (
            "/stats/summer-league/explorer"
            f"?subject=competitions&profile_scope=season&detail_year={scope.year}"
        )
    return (
        "/stats/summer-league/explorer"
        "?subject=competitions&profile_scope=competition"
        f"&competition_id={scope.competition_id}"
    )


def _page_metric_view(
    profile: EnvironmentProfile, definition: MetricDefinition
) -> PageMetricView:
    """Resolve one registry metric for ``profile`` into a page-module view."""
    raw = registry_raw_value(profile, definition)
    info = metric_coverage_for_profile(profile, definition)
    return PageMetricView(
        key=definition.key,
        label=definition.label,
        formatted_value=format_metric_value(definition.key, raw),
        unit=definition.unit.value,
        formula=definition.formula,
        denominator=definition.denominator,
        interpretation=definition.interpretation,
        confidence_note=definition.confidence_note,
        coverage=info.coverage,
        covered=info.covered,
        eligible=info.eligible,
        reason=info.reason,
    )


def build_profile_summary_view(
    profile: EnvironmentProfile,
    *,
    stale_after_hours: Optional[int] = None,
) -> ProfileSummaryView:
    """Build the season-hub / venue-page summary DTO for one current profile.

    Reads only the already-fetched ``profile`` row plus the shared registry —
    no additional query (contract §9). Every metric's value/coverage/
    definition is produced by the same primitives the Explorer detail panel
    uses, so the module can never publish a divergent number (contract §7).

    Args:
        profile: A current :class:`SummerLeagueEnvironmentProfile` row,
            already resolved by the caller via
            :func:`get_current_profile_by_scope_key`.
        stale_after_hours: Staleness threshold override (tests only);
            defaults to the live
            ``settings.summer_league_environment_stale_after_hours`` via
            :func:`~app.services.summer_league_environment_registry.is_profile_stale`.

    Returns:
        A :class:`ProfileSummaryView` ready for the shared
        ``competition_context_module`` Jinja macro.
    """
    scope = EnvironmentScope(
        scope_kind="season_all_competitions"
        if profile.scope_kind == SCOPE_KIND_SEASON
        else "competition",
        year=profile.year,
        competition_id=profile.competition_id,
        scope_key=profile.scope_key,
    )
    env_metrics = [
        _page_metric_view(profile, get_metric(key))
        for key in _HEADLINE_ENVIRONMENT_KEYS
    ]
    composition_metrics = [
        _page_metric_view(profile, get_metric(key))
        for key in _HEADLINE_COMPOSITION_KEYS
    ]
    is_stale = is_profile_stale(
        profile.calculated_at, stale_after_hours=stale_after_hours
    )
    return ProfileSummaryView(
        scope_key=profile.scope_key,
        scope_kind=profile.scope_kind,
        year=profile.year,
        competition_id=profile.competition_id,
        venue_slug=profile.venue_slug,
        display_name=profile.display_name,
        version=profile.version,
        registry_version=profile.registry_version,
        calculation_version=profile.calculation_version,
        calculated_at=profile.calculated_at,
        source_watermark=profile.source_watermark,
        is_stale=is_stale,
        included_competitions=profile.included_competitions,
        final_games=profile.final_games,
        scheduled_games=profile.scheduled_games,
        distinct_teams=profile.distinct_teams,
        appeared_players=profile.appeared_players,
        appeared_unresolved=profile.appeared_unresolved,
        explorer_href=explorer_competitions_href(scope),
        sections=[
            PageMetricSectionView(
                key="environment", label="How it played", metrics=env_metrics
            ),
            PageMetricSectionView(
                key="composition",
                label="Field composition",
                metrics=composition_metrics,
            ),
        ],
    )


# ===========================================================================
# #617 — deterministic set-based aggregation, coverage, and publication.
# ===========================================================================


def _age_years(birthdate: date, reference: date) -> float:
    """Age in years at ``reference`` for a player born on ``birthdate``."""
    return (reference - birthdate).days / 365.25


def _max_dt(
    current: Optional[datetime], candidate: Optional[datetime]
) -> Optional[datetime]:
    """Return the later of two optional datetimes."""
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


@dataclass
class _SourceProvenance:
    """Row count + freshness watermark for one contributing spoke source."""

    row_count: int = 0
    watermark: Optional[datetime] = None

    def observe(self, updated_at: Optional[datetime], *, rows: int = 1) -> None:
        """Record ``rows`` contributing rows and advance the watermark."""
        self.row_count += rows
        self.watermark = _max_dt(self.watermark, updated_at)


@dataclass
class _CompetitionInputs:
    """All eligible-final-game facts for one competition, before rate math.

    Numerators/denominators are pooled here; a season scope sums many of these.
    Every dict is keyed by canonical ``player_id`` (resolved) or, for unresolved
    appearances, the negated ``source_player_id`` so distinct unresolved people
    are never collapsed into one another or into a resolved player.
    """

    competition_id: int
    year: int
    venue_slug: Optional[str]
    display_name: str
    starts_on: Optional[date]
    ends_on: Optional[date] = None

    # Exact raw-source reference (contract: "trace to exact contributing raw
    # run/source references") -- the audited scrape manifest this
    # competition's normalized facts came from, if any.
    raw_run_id: Optional[int] = None
    # Worst-case SummerLeagueRawRun.status for that manifest.
    raw_run_status: Optional[str] = None
    # Worst-case SummerLeagueRawFile.parse_status per source kind ("box" /
    # "shot" / "pbp") across this competition's eligible final games.
    parse_status_by_source: dict[str, str] = field(default_factory=dict)
    # Per-game raw-file parse status: internal game id -> {"box"/"shot"/"pbp":
    # worst-case SummerLeagueRawFileStatus.value for that one game}. This is
    # the audited certification signal shot/PBP completeness reads from --
    # not the mere existence of a normalized shot/PBP event row (contract §3;
    # coverage audit: "reconcile against summer_league_raw_files.parse_status
    # ... before certifying shot_complete").
    game_parse_status: dict[int, dict[str, str]] = field(
        default_factory=lambda: defaultdict(dict)
    )

    # Schedule / status disclosure (all statuses).
    final_games: int = 0
    scheduled_games: int = 0
    other_games: int = 0

    # Pooled team box over paired (box-complete) final games.
    pooled_box: Box = field(default_factory=Box)
    team_game_rows: int = 0
    team_minutes: float = 0.0
    total_possessions: float = 0.0
    team_ortgs: list[float] = field(default_factory=list)
    team_points: list[float] = field(default_factory=list)
    box_complete_games: int = 0

    # Score / overtime disclosure (final games).
    games_with_score: int = 0
    margin_abs_sum: float = 0.0
    close_games: int = 0
    games_with_known_ot: int = 0
    overtime_games: int = 0

    # Shot-chart pooling (final games).
    shot_covered_game_ids: set[int] = field(default_factory=set)
    rim_fga: int = 0
    rim_fgm: int = 0
    mapped_fga: int = 0

    # Play-by-play (informational).
    pbp_covered_game_ids: set[int] = field(default_factory=set)

    # Appeared-player facts (positive minutes in a final game).
    minutes_by_identity: dict[int, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    points_by_identity: dict[int, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    first_date_by_player: dict[int, date] = field(default_factory=dict)
    resolved_player_ids: set[int] = field(default_factory=set)
    unresolved_source_ids: set[int] = field(default_factory=set)
    event_position_by_player: dict[int, str] = field(default_factory=dict)
    player_game_rows: int = 0
    participation_count: int = 0
    team_entry_ids: set[int] = field(default_factory=set)

    provenance: dict[str, _SourceProvenance] = field(
        default_factory=lambda: defaultdict(_SourceProvenance)
    )


async def _target_competitions(
    db: AsyncSession,
    *,
    year: Optional[int],
    competition_id: Optional[int],
) -> list[SummerLeagueEdition]:
    """Resolve the competitions in scope for a rebuild request (set-based)."""
    stmt = select(SummerLeagueEdition)
    if competition_id is not None:
        stmt = stmt.where(col(SummerLeagueEdition.id) == competition_id)
    elif year is not None:
        stmt = stmt.where(col(SummerLeagueEdition.year) == year)
    stmt = stmt.order_by(col(SummerLeagueEdition.id))
    return list((await db.execute(stmt)).scalars())


async def _load_competition_inputs(
    db: AsyncSession, competitions: list[SummerLeagueEdition]
) -> dict[int, _CompetitionInputs]:
    """Load every eligible-final-game fact for ``competitions`` in bulk.

    Issues a fixed, small number of set-based grouped queries (never one per
    competition or player) and assembles per-competition accumulators in memory,
    mirroring the offline materialization boundary in
    :mod:`app.services.summer_league.metrics`.
    """
    inputs: dict[int, _CompetitionInputs] = {
        int(c.id): _CompetitionInputs(  # type: ignore[arg-type]
            competition_id=int(c.id),  # type: ignore[arg-type]
            year=c.year,
            venue_slug=c.venue_slug,
            display_name=c.display_name,
            starts_on=c.starts_on,
            ends_on=c.ends_on,
            raw_run_id=c.raw_run_id,
        )
        for c in competitions
    }
    comp_ids = list(inputs.keys())
    if not comp_ids:
        return inputs

    await _load_game_status(db, comp_ids, inputs)
    # Per-game raw-file parse status must be loaded before shot/PBP pooling
    # (both certify from it, not from event-row existence -- contract §3).
    await _load_game_parse_status(db, comp_ids, inputs)
    await _load_team_boxes(db, comp_ids, inputs)
    await _load_appearances(db, comp_ids, inputs)
    await _load_participation(db, comp_ids, inputs)
    await _load_shots(db, comp_ids, inputs)
    await _load_pbp(db, comp_ids, inputs)
    await _load_raw_run_status(db, inputs)
    return inputs


async def _load_game_status(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Bucket every game by status and pool final-game score/OT disclosure."""
    game = SummerLeagueGame
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                game.competition_id,
                game.status,
                game.home_score,
                game.away_score,
                game.status_text,
                game.updated_at,
            ).where(col(game.competition_id).in_(comp_ids))
        )
    ).all()
    for comp_id, status, home, away, status_text, updated_at in rows:
        acc = inputs[int(comp_id)]
        # Every game (any status) is a profile-affecting input: a game
        # flipping scheduled -> final changes eligibility/coverage even
        # before it carries a score, so the watermark must move with it
        # regardless of the score/ot_state observations below (which only
        # fire for FINAL games).
        acc.provenance["schedule"].observe(updated_at)
        if status == SummerLeagueGameStatus.FINAL:
            acc.final_games += 1
            acc.provenance["score"].observe(updated_at)
            if home is not None and away is not None:
                margin = abs(int(home) - int(away))
                acc.games_with_score += 1
                acc.margin_abs_sum += margin
                if margin <= CLOSE_GAME_MARGIN:
                    acc.close_games += 1
            if status_text is not None:
                acc.games_with_known_ot += 1
                acc.provenance["ot_state"].observe(updated_at)
                if "OT" in status_text.upper():
                    acc.overtime_games += 1
        elif status == SummerLeagueGameStatus.SCHEDULED:
            acc.scheduled_games += 1
        else:
            acc.other_games += 1


async def _load_team_boxes(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Pool paired team boxes from box-complete final games (opponent-adjusted).

    A final game contributes only when it has exactly two *usable* team-box
    rows: both clear the regulation-minute floor **and** carry every
    metric-required field non-null (:func:`_box_row_usable`) -- a row with an
    unparsed field must never reach ``Box.add_row`` and silently zero-fill
    (contract §3). Possessions reuse :meth:`Box.poss` — the shared
    opponent-adjusted estimate — so pace and ORtg never invent a competing
    possession formula (contract §4). Turnover rate deliberately does **not**
    use this possession estimate; its denominator is the frozen
    ``FGA + 0.44*FTA + TOV`` plays formula, computed independently in
    :func:`_environment_metric_values`.
    """
    tgl = SummerLeagueTeamGameLog
    game = SummerLeagueGame
    box_fields = (
        "minutes",
        "pts",
        "fgm",
        "fga",
        "fg3m",
        "fg3a",
        "ftm",
        "fta",
        "oreb",
        "dreb",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "pf",
    )
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                tgl.competition_id,
                tgl.game_id,
                tgl.team_entry_id,
                *[getattr(tgl, f) for f in box_fields],
                tgl.updated_at,
            )
            .join(game, col(game.id) == col(tgl.game_id))
            .where(
                col(game.status) == SummerLeagueGameStatus.FINAL,
                col(tgl.competition_id).in_(comp_ids),
            )
        )
    ).all()
    # game_id -> list of (competition_id, team_entry_id, Box, minutes,
    # updated_at, usable) -- ``usable`` is decided from the raw row (contract
    # §3), before any field can be lost to Box.add_row's null-to-zero fold.
    by_game: dict[int, list[tuple[int, int, Box, float, Any, bool]]] = defaultdict(list)
    for row in rows:
        box = Box()
        box.add_row(row)
        by_game[int(row.game_id)].append(
            (
                int(row.competition_id),
                int(row.team_entry_id),
                box,
                float(row.minutes or 0),
                row.updated_at,
                _box_row_usable(row),
            )
        )
    for entries in by_game.values():
        if len(entries) != 2:
            continue
        (
            (comp_a, team_a, box_a, min_a, upd_a, usable_a),
            (comp_b, team_b, box_b, min_b, upd_b, usable_b),
        ) = entries
        if not (usable_a and usable_b):
            continue
        acc = inputs[comp_a]
        acc.box_complete_games += 1
        for team_id, box, minutes, opp, updated_at in (
            (team_a, box_a, min_a, box_b, upd_a),
            (team_b, box_b, min_b, box_a, upd_b),
        ):
            acc.pooled_box.add_row(box)
            acc.team_game_rows += 1
            acc.team_minutes += minutes
            acc.team_entry_ids.add(team_id)
            poss = box.poss(opp)
            acc.total_possessions += poss
            if (team_ortg := points_per_100(box.pts, poss)) is not None:
                acc.team_ortgs.append(team_ortg)
            acc.team_points.append(float(box.pts))
            acc.provenance["box"].observe(updated_at)


async def _load_appearances(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Pool appeared-player minutes/points and identity from final games.

    An appearance is a player-game log in a final game with positive minutes; DNP
    shells (0 minutes) never count (contract §5). Resolved people pool by
    canonical ``player_id``; each distinct unresolved ``source_player_id`` is kept
    separate (keyed by its negation) and disclosed, never collapsed.
    """
    pgl = SummerLeaguePlayerGameLog
    game = SummerLeagueGame
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload, misc]
                pgl.competition_id,
                pgl.player_id,
                pgl.source_player_id,
                pgl.minutes_seconds,
                pgl.pts,
                pgl.starter_position,
                pgl.team_entry_id,
                game.game_date,
                pgl.updated_at,
            )
            .join(game, col(game.id) == col(pgl.game_id))
            .where(
                col(game.status) == SummerLeagueGameStatus.FINAL,
                col(pgl.minutes_seconds) > 0,
                col(pgl.competition_id).in_(comp_ids),
            )
        )
    ).all()
    for row in rows:
        acc = inputs[int(row.competition_id)]
        acc.player_game_rows += 1
        acc.provenance["identity"].observe(row.updated_at)
        minutes = float(row.minutes_seconds or 0) / 60.0
        points = float(row.pts or 0)
        if row.player_id is not None:
            player_id = int(row.player_id)
            identity = player_id
            acc.resolved_player_ids.add(player_id)
            if row.starter_position and player_id not in acc.event_position_by_player:
                acc.event_position_by_player[player_id] = str(row.starter_position)
            game_date = row.game_date
            if game_date is not None:
                current = acc.first_date_by_player.get(player_id)
                if current is None or game_date < current:
                    acc.first_date_by_player[player_id] = game_date
        else:
            identity = -int(row.source_player_id)
            acc.unresolved_source_ids.add(int(row.source_player_id))
        acc.minutes_by_identity[identity] += minutes
        acc.points_by_identity[identity] += points


async def _load_participation(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Count participation rows and capture event-time roster position (preferred).

    Participation (roster/stint assertions) can change a profile's position
    composition independent of any game log -- a roster correction must
    advance the input watermark even when no player-game log changed, so this
    observes its own ``"participation"`` provenance source (contract:
    "ensure the input watermark covers every input that can change a
    profile").
    """
    part = SummerLeagueParticipation
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                part.competition_id,
                part.player_id,
                part.roster_position,
                part.updated_at,
            ).where(col(part.competition_id).in_(comp_ids))
        )
    ).all()
    for comp_id, player_id, roster_position, updated_at in rows:
        acc = inputs[int(comp_id)]
        acc.participation_count += 1
        acc.provenance["participation"].observe(updated_at)
        if player_id is not None and roster_position:
            # Roster position is the strongest event-time signal; it wins over a
            # box starter_position captured for a single game.
            acc.event_position_by_player[int(player_id)] = str(roster_position)


async def _load_shots(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Pool rim / mapped shot attempts and certify which final games are covered.

    A game certifies as shot-covered only when its shotchartdetail raw file
    parsed successfully (:data:`SummerLeagueRawFileStatus.PARSED`, from
    :func:`_load_game_parse_status`) **and** every shot row in that game maps
    to a known non-backcourt/backcourt zone. A null or unrecognized zone, or a
    parse status short of ``PARSED`` (missing evidence included), is treated
    as a certification failure for the *whole game* -- one partial/unmapped
    row must not certify an otherwise-uncertain shot chart (contract §3).
    """
    shot = SummerLeagueShotEvent
    game = SummerLeagueGame
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                shot.competition_id,
                shot.game_id,
                shot.shot_zone_basic,
                shot.made,
                shot.updated_at,
            )
            .join(game, col(game.id) == col(shot.game_id))
            .where(
                col(game.status) == SummerLeagueGameStatus.FINAL,
                col(shot.competition_id).in_(comp_ids),
            )
        )
    ).all()
    seen_games: set[tuple[int, int]] = set()
    unmapped_games: set[tuple[int, int]] = set()
    for comp_id, game_id, zone, made, updated_at in rows:
        acc = inputs[int(comp_id)]
        key = (int(comp_id), int(game_id))
        seen_games.add(key)
        acc.provenance["shot"].observe(updated_at)
        if zone is None or zone not in _KNOWN_SHOT_ZONES:
            unmapped_games.add(key)
            continue
        if zone == BACKCOURT_ZONE:
            continue
        acc.mapped_fga += 1
        if zone == RIM_ZONE:
            acc.rim_fga += 1
            if made:
                acc.rim_fgm += 1
    for comp_id, game_id in seen_games:
        if (comp_id, game_id) in unmapped_games:
            continue
        parse_status = inputs[comp_id].game_parse_status.get(game_id, {}).get("shot")
        if parse_status == SummerLeagueRawFileStatus.PARSED.value:
            inputs[comp_id].shot_covered_game_ids.add(game_id)


async def _load_pbp(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Certify which final games have successfully parsed play-by-play.

    PBP is informational only in v1 (gates no displayed metric), but its own
    coverage badge/filter must still be honest: a game counts as PBP-covered
    only when its playbyplayv2 raw file parsed successfully (contract §3),
    not merely because at least one normalized event row exists for it.
    """
    pbp = SummerLeaguePlayByPlayEvent
    game = SummerLeagueGame
    rows = (
        await db.execute(
            select(pbp.competition_id, pbp.game_id, func.max(pbp.updated_at))  # type: ignore[call-overload]
            .join(game, col(game.id) == col(pbp.game_id))
            .where(
                col(game.status) == SummerLeagueGameStatus.FINAL,
                col(pbp.competition_id).in_(comp_ids),
            )
            .group_by(pbp.competition_id, pbp.game_id)
        )
    ).all()
    for comp_id, game_id, updated_at in rows:
        acc = inputs[int(comp_id)]
        gid = int(game_id)
        acc.provenance["pbp"].observe(updated_at)
        parse_status = acc.game_parse_status.get(gid, {}).get("pbp")
        if parse_status == SummerLeagueRawFileStatus.PARSED.value:
            acc.pbp_covered_game_ids.add(gid)


# Worst-first ranking for SummerLeagueRawRunStatus: the pooled/aggregated
# status across every contributing manifest is the single worst one, never
# silently averaged away.
_RAW_RUN_STATUS_RANK: dict[SummerLeagueRawRunStatus, int] = {
    SummerLeagueRawRunStatus.COMPLETE: 0,
    SummerLeagueRawRunStatus.PENDING: 1,
    SummerLeagueRawRunStatus.PARTIAL: 2,
    SummerLeagueRawRunStatus.FAILED: 3,
}

# Worst-first ranking for SummerLeagueRawFileStatus (per-endpoint parse
# status). PRESENT (fetched, not yet parsed) ranks worse than a normal
# PARSED/SKIPPED outcome but better than a genuine gap/failure.
_PARSE_STATUS_RANK: dict[SummerLeagueRawFileStatus, int] = {
    SummerLeagueRawFileStatus.PARSED: 0,
    SummerLeagueRawFileStatus.SKIPPED: 1,
    SummerLeagueRawFileStatus.PRESENT: 2,
    SummerLeagueRawFileStatus.EMPTY: 3,
    SummerLeagueRawFileStatus.MISSING: 4,
    SummerLeagueRawFileStatus.PARSE_FAILED: 5,
}

# String-keyed mirrors of the rank tables above: `_CompetitionInputs`/
# `_PooledScope` store the enum's ``.value`` (a plain string, matching the
# provenance table's ``Optional[str]`` columns) rather than the enum itself.
_RAW_RUN_STATUS_VALUE_RANK: dict[str, int] = {
    status.value: rank for status, rank in _RAW_RUN_STATUS_RANK.items()
}
_PARSE_STATUS_VALUE_RANK: dict[str, int] = {
    status.value: rank for status, rank in _PARSE_STATUS_RANK.items()
}


def _worse_status(current: Optional[str], candidate: str, rank: dict[str, int]) -> str:
    """Return whichever of ``current``/``candidate`` ranks worse (never averaged)."""
    if current is None or rank.get(candidate, 0) > rank.get(current, 0):
        return candidate
    return current


# SummerLeagueRawFile.endpoint -> the Competition Context source kind it
# feeds (single source of truth mirroring app.services.summer_league.
# raw_ingestion.GAME_ENDPOINTS' box/shot/pbp split).
_BOX_RAW_ENDPOINTS = (
    "boxscoretraditionalv2",
    "boxscoreadvancedv2",
    "boxscorescoringv2",
)
_SHOT_RAW_ENDPOINT = "shotchartdetail"
_PBP_RAW_ENDPOINT = "playbyplayv2"
_ENDPOINT_SOURCE_KIND: dict[str, str] = {
    **{endpoint: "box" for endpoint in _BOX_RAW_ENDPOINTS},
    _SHOT_RAW_ENDPOINT: "shot",
    _PBP_RAW_ENDPOINT: "pbp",
}


async def _load_raw_run_status(
    db: AsyncSession, inputs: dict[int, _CompetitionInputs]
) -> None:
    """Bulk-load each contributing competition's raw-run (source) status.

    One set-based query keyed by the distinct ``raw_run_id`` values already
    captured on ``inputs`` (from ``SummerLeagueEdition.raw_run_id``) --
    populates the "source status" disclosure (contract: "populate ... source
    status where modeled").
    """
    raw_run_ids = {
        acc.raw_run_id for acc in inputs.values() if acc.raw_run_id is not None
    }
    if not raw_run_ids:
        return
    rows = (
        await db.execute(
            select(SummerLeagueRawRun.id, SummerLeagueRawRun.status).where(  # type: ignore[call-overload]
                col(SummerLeagueRawRun.id).in_(raw_run_ids)
            )
        )
    ).all()
    status_by_run: dict[int, SummerLeagueRawRunStatus] = {
        int(run_id): status for run_id, status in rows
    }
    for acc in inputs.values():
        if acc.raw_run_id is not None and acc.raw_run_id in status_by_run:
            acc.raw_run_status = status_by_run[acc.raw_run_id].value


async def _load_game_parse_status(
    db: AsyncSession, comp_ids: list[int], inputs: dict[int, _CompetitionInputs]
) -> None:
    """Bulk-load per-game, then per-competition, raw-file parse status.

    Joins ``SummerLeagueRawFile`` to eligible final games by
    ``nba_stats_game_id`` (unique) and reduces to the worst-case
    :class:`SummerLeagueRawFileStatus` per **(game, source kind)** --
    populating :attr:`_CompetitionInputs.game_parse_status`, the audited
    certification signal :func:`_load_shots`/:func:`_load_pbp` gate on
    (contract §3: "a successfully parsed shot-chart/PBP input", not the mere
    existence of a normalized event row). The same reduction rolls up to the
    worst-case per (competition, source kind) in
    :attr:`_CompetitionInputs.parse_status_by_source`, which feeds the
    provenance "parse status" disclosure. Score/OT-state/identity/
    participation/schedule have no directly-modeled per-file parse status and
    are left absent from both dicts.

    A raw file's own ``raw_run_id`` is matched against the competition's
    pinned ``SummerLeagueEdition.raw_run_id`` (the exact scrape manifest
    ``raw_run_ids`` provenance already traces the profile to -- see
    ``_load_raw_run_status``). Without this, a stale file left behind by an
    older/failed scrape re-run for the same NBA game id would pool into the
    worst-case reduction alongside the file the current run actually
    produced, silently uncertifying (or, worse, certifying) a game based on
    a manifest that isn't the one contributing to this profile. Competitions
    with no pinned ``raw_run_id`` (pre-audit legacy data) fall back to the
    unscoped, game-id-only match.
    """
    raw_file = SummerLeagueRawFile
    game = SummerLeagueGame
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                game.competition_id,
                game.id,
                raw_file.endpoint,
                raw_file.parse_status,
                raw_file.raw_run_id,
            )
            .join(game, col(game.nba_stats_game_id) == col(raw_file.game_id))
            .where(
                col(game.status) == SummerLeagueGameStatus.FINAL,
                col(game.competition_id).in_(comp_ids),
                col(raw_file.endpoint).in_(_ENDPOINT_SOURCE_KIND),
            )
        )
    ).all()
    pinned_run_by_comp = {comp_id: acc.raw_run_id for comp_id, acc in inputs.items()}
    worst_per_game: dict[tuple[int, str], SummerLeagueRawFileStatus] = {}
    comp_by_game: dict[int, int] = {}
    for comp_id, game_id, endpoint, parse_status, file_run_id in rows:
        source_kind = _ENDPOINT_SOURCE_KIND.get(endpoint)
        if source_kind is None:
            continue
        comp_id = int(comp_id)
        pinned_run_id = pinned_run_by_comp.get(comp_id)
        if pinned_run_id is not None and int(file_run_id) != pinned_run_id:
            continue
        gid = int(game_id)
        comp_by_game[gid] = comp_id
        key = (gid, source_kind)
        current = worst_per_game.get(key)
        if (
            current is None
            or _PARSE_STATUS_RANK[parse_status] > _PARSE_STATUS_RANK[current]
        ):
            worst_per_game[key] = parse_status

    worst_per_comp: dict[tuple[int, str], SummerLeagueRawFileStatus] = {}
    for (game_id, source_kind), status in worst_per_game.items():
        comp_id = comp_by_game[game_id]
        inputs[comp_id].game_parse_status[game_id][source_kind] = status.value
        comp_key = (comp_id, source_kind)
        current_comp = worst_per_comp.get(comp_key)
        if (
            current_comp is None
            or _PARSE_STATUS_RANK[status] > _PARSE_STATUS_RANK[current_comp]
        ):
            worst_per_comp[comp_key] = status
    for (comp_id, source_kind), status in worst_per_comp.items():
        inputs[comp_id].parse_status_by_source[source_kind] = status.value


@dataclass
class _PlayerAttributes:
    """Canonical, event-independent attributes for one appeared player."""

    birthdate: Optional[date] = None
    draft_year: Optional[int] = None
    draft_round: Optional[int] = None
    draft_pick: Optional[int] = None
    canonical_position: Optional[str] = None
    first_sl_year: Optional[int] = None
    # Every distinct Summer League calendar year the player has appeared in
    # (positive minutes, any competition, FINAL game), sorted ascending --
    # global across every competition, not just those in the current rebuild
    # request. A profile's appearance-number distribution (contract §5:
    # "appearance rank is the player's distinct Summer League calendar-year
    # rank across all competitions") derives the rank for a given scope year
    # as ``count(y for y in sl_years if y <= scope_year)`` -- computed once
    # here and reused for every scope year a rebuild touches.
    sl_years: tuple[int, ...] = ()


async def _load_player_attributes(
    db: AsyncSession, player_ids: set[int]
) -> dict[int, _PlayerAttributes]:
    """Bulk-load draft / birthdate / canonical position for all appeared players."""
    attributes: dict[int, _PlayerAttributes] = {
        pid: _PlayerAttributes() for pid in player_ids
    }
    if not player_ids:
        return attributes
    ids = list(player_ids)
    rows = (
        await db.execute(
            select(  # type: ignore[call-overload, misc]
                PlayerMaster.id,
                PlayerMaster.birthdate,
                PlayerMaster.draft_year,
                PlayerMaster.draft_round,
                PlayerMaster.draft_pick,
                Position.code,
            )
            .outerjoin(
                PlayerStatus, col(PlayerStatus.player_id) == col(PlayerMaster.id)
            )
            .outerjoin(Position, col(Position.id) == col(PlayerStatus.position_id))
            .where(col(PlayerMaster.id).in_(ids))
        )
    ).all()
    for pid, birthdate, draft_year, draft_round, draft_pick, position_code in rows:
        attributes[int(pid)] = _PlayerAttributes(
            birthdate=birthdate,
            draft_year=draft_year,
            draft_round=draft_round,
            draft_pick=draft_pick,
            canonical_position=position_code,
        )
    # Rookie/returner and the appearance-number distribution both need every
    # distinct Summer League calendar year a player has appeared in *across
    # all competitions* (not just those in the current rebuild request): one
    # set-based query, deduplicated in Python rather than two separate
    # aggregate queries.
    pgl = SummerLeaguePlayerGameLog
    game = SummerLeagueGame
    year_rows = (
        await db.execute(
            select(pgl.player_id, SummerLeagueEdition.year)  # type: ignore[call-overload]
            .join(game, col(game.id) == col(pgl.game_id))
            .join(
                SummerLeagueEdition,
                col(SummerLeagueEdition.id) == col(pgl.competition_id),
            )
            .where(
                col(game.status) == SummerLeagueGameStatus.FINAL,
                col(pgl.minutes_seconds) > 0,
                col(pgl.player_id).in_(ids),
            )
            .distinct()
        )
    ).all()
    years_by_player: dict[int, set[int]] = defaultdict(set)
    for pid, year in year_rows:
        if pid is not None:
            years_by_player[int(pid)].add(int(year))
    for pid, years in years_by_player.items():
        if pid in attributes:
            ordered = tuple(sorted(years))
            attributes[pid].sl_years = ordered
            attributes[pid].first_sl_year = ordered[0]
    return attributes


# ---------------------------------------------------------------------------
# Pooling + metric / coverage / field-composition calculation.
# ---------------------------------------------------------------------------


@dataclass
class _PooledScope:
    """One scope's pooled inputs (a competition, or a season of competitions)."""

    scope: EnvironmentScope
    display_name: str
    venue_slug: Optional[str]
    members: list[_CompetitionInputs]

    # Pooled game/box/score/shot/pbp totals.
    final_games: int = 0
    scheduled_games: int = 0
    other_games: int = 0
    box_complete_games: int = 0
    shot_covered_games: int = 0
    pbp_covered_games: int = 0
    games_with_score: int = 0
    games_with_known_ot: int = 0
    pooled_box: Box = field(default_factory=Box)
    team_game_rows: int = 0
    team_minutes: float = 0.0
    total_possessions: float = 0.0
    team_ortgs: list[float] = field(default_factory=list)
    team_points: list[float] = field(default_factory=list)
    margin_abs_sum: float = 0.0
    close_games: int = 0
    overtime_games: int = 0
    rim_fga: int = 0
    rim_fgm: int = 0
    mapped_fga: int = 0

    # Pooled appeared-player facts keyed across every member competition.
    minutes_by_identity: dict[int, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    points_by_identity: dict[int, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    first_date_by_player: dict[int, date] = field(default_factory=dict)
    resolved_player_ids: set[int] = field(default_factory=set)
    unresolved_source_ids: set[int] = field(default_factory=set)
    event_position_by_player: dict[int, str] = field(default_factory=dict)
    player_game_rows: int = 0
    participation_count: int = 0
    team_entry_ids: set[int] = field(default_factory=set)
    provenance: dict[str, _SourceProvenance] = field(
        default_factory=lambda: defaultdict(_SourceProvenance)
    )

    # Exact raw-source references (contract: trace a profile to exact
    # contributing raw run/source references) and their aggregated status
    # disclosures (contract: populate parse status and source status where
    # modeled).
    raw_run_ids: set[int] = field(default_factory=set)
    raw_run_status: Optional[str] = None
    parse_status_by_source: dict[str, str] = field(default_factory=dict)

    def pool(self) -> None:
        """Sum every member competition's numerators/denominators into this scope."""
        for member in self.members:
            self.final_games += member.final_games
            self.scheduled_games += member.scheduled_games
            self.other_games += member.other_games
            self.box_complete_games += member.box_complete_games
            self.shot_covered_games += len(member.shot_covered_game_ids)
            self.pbp_covered_games += len(member.pbp_covered_game_ids)
            self.games_with_score += member.games_with_score
            self.games_with_known_ot += member.games_with_known_ot
            self.pooled_box.add_row(member.pooled_box)
            self.team_game_rows += member.team_game_rows
            self.team_minutes += member.team_minutes
            self.total_possessions += member.total_possessions
            self.team_ortgs.extend(member.team_ortgs)
            self.team_points.extend(member.team_points)
            self.margin_abs_sum += member.margin_abs_sum
            self.close_games += member.close_games
            self.overtime_games += member.overtime_games
            self.rim_fga += member.rim_fga
            self.rim_fgm += member.rim_fgm
            self.mapped_fga += member.mapped_fga
            self.player_game_rows += member.player_game_rows
            self.participation_count += member.participation_count
            self.team_entry_ids |= member.team_entry_ids
            self.resolved_player_ids |= member.resolved_player_ids
            self.unresolved_source_ids |= member.unresolved_source_ids
            for identity, minutes in member.minutes_by_identity.items():
                self.minutes_by_identity[identity] += minutes
            for identity, points in member.points_by_identity.items():
                self.points_by_identity[identity] += points
            for player_id, day in member.first_date_by_player.items():
                current = self.first_date_by_player.get(player_id)
                if current is None or day < current:
                    self.first_date_by_player[player_id] = day
            for player_id, position in member.event_position_by_player.items():
                self.event_position_by_player.setdefault(player_id, position)
            for source_kind, prov in member.provenance.items():
                bucket = self.provenance[source_kind]
                bucket.row_count += prov.row_count
                bucket.watermark = _max_dt(bucket.watermark, prov.watermark)
            if member.raw_run_id is not None:
                self.raw_run_ids.add(member.raw_run_id)
            if member.raw_run_status is not None:
                self.raw_run_status = _worse_status(
                    self.raw_run_status,
                    member.raw_run_status,
                    _RAW_RUN_STATUS_VALUE_RANK,
                )
            for source_kind, status in member.parse_status_by_source.items():
                current_status = self.parse_status_by_source.get(source_kind)
                self.parse_status_by_source[source_kind] = _worse_status(
                    current_status, status, _PARSE_STATUS_VALUE_RANK
                )

    @property
    def watermark(self) -> Optional[datetime]:
        """The freshest contributing source-row timestamp across the scope."""
        latest: Optional[datetime] = None
        for prov in self.provenance.values():
            latest = _max_dt(latest, prov.watermark)
        return latest


def _coverage_verdict(covered: int, eligible: int) -> str:
    """Map a covered/eligible pair to the frozen coverage vocabulary."""
    if eligible <= 0 or covered <= 0:
        return COVERAGE_UNAVAILABLE
    if covered >= eligible:
        return COVERAGE_COMPLETE
    return COVERAGE_PARTIAL


def _coverage_for_source(
    pooled: _PooledScope, source: CoverageSource
) -> tuple[str, int, int]:
    """Return ``(verdict, covered, eligible)`` for a metric's coverage source."""
    final_games = pooled.final_games
    if source is CoverageSource.BOX:
        return (
            _coverage_verdict(pooled.box_complete_games, final_games),
            (pooled.box_complete_games),
            final_games,
        )
    if source is CoverageSource.SHOT:
        return (
            _coverage_verdict(pooled.shot_covered_games, final_games),
            pooled.shot_covered_games,
            final_games,
        )
    if source is CoverageSource.SCORE:
        return (
            _coverage_verdict(pooled.games_with_score, final_games),
            pooled.games_with_score,
            final_games,
        )
    if source is CoverageSource.OT_STATE:
        return (
            _coverage_verdict(pooled.games_with_known_ot, final_games),
            pooled.games_with_known_ot,
            final_games,
        )
    if source is CoverageSource.PBP:
        return (
            _coverage_verdict(pooled.pbp_covered_games, final_games),
            pooled.pbp_covered_games,
            final_games,
        )
    # IDENTITY: an appeared player is "covered" when resolved to a canonical id.
    resolved = len(pooled.resolved_player_ids)
    eligible = resolved + len(pooled.unresolved_source_ids)
    return _coverage_verdict(resolved, eligible), resolved, eligible


def _environment_metric_values(pooled: _PooledScope) -> dict[str, Optional[float]]:
    """Compute every box/shot/score/OT/landscape metric from pooled totals.

    Rates return ``None`` on a zero denominator (never ``0.0``) so an undefined
    number is disclosed as unknown. Possessions are the pooled opponent-adjusted
    :meth:`Box.poss` totals — the single shared possession estimate.
    """
    box = pooled.pooled_box
    poss = pooled.total_possessions
    team_games = pooled.team_game_rows
    values: dict[str, Optional[float]] = {}

    values["points_per_team_game"] = safe_ratio(box.pts, team_games)
    values["estimated_possessions"] = safe_ratio(poss, team_games)
    values["pace_per_48"] = pace_per_48(poss, pooled.team_minutes)
    values["offensive_rating"] = points_per_100(box.pts, poss)
    values["three_attempt_share"] = safe_ratio(box.fg3a, box.fga)
    values["three_fg_pct"] = safe_ratio(box.fg3m, box.fg3a)
    values["free_throw_rate"] = safe_ratio(box.fta, box.fga)
    values["offensive_rebound_rate"] = safe_ratio(box.oreb, box.oreb + box.dreb)
    # Frozen contract formula (§4): FGA + 0.44*FTA + TOV, not the pooled
    # opponent-adjusted possession estimate (`poss`) used for pace/ORtg above.
    values["turnover_rate"] = safe_ratio(box.tov, box.fga + 0.44 * box.fta + box.tov)
    values["assisted_fg_rate"] = safe_ratio(box.ast, box.fgm)
    values["rim_attempt_share"] = safe_ratio(pooled.rim_fga, pooled.mapped_fga)
    values["rim_fg_pct"] = safe_ratio(pooled.rim_fgm, pooled.rim_fga)
    values["average_score_margin"] = safe_ratio(
        pooled.margin_abs_sum, pooled.games_with_score
    )
    values["close_game_share"] = safe_ratio(pooled.close_games, pooled.games_with_score)
    values["overtime_share"] = safe_ratio(
        pooled.overtime_games, pooled.games_with_known_ot
    )

    # Performance landscape.
    if len(pooled.team_ortgs) >= MIN_LANDSCAPE_SAMPLE:
        values["team_ortg_iqr"] = _percentile(pooled.team_ortgs, 0.75) - _percentile(
            pooled.team_ortgs, 0.25
        )
    else:
        values["team_ortg_iqr"] = None
    # Scoring distribution: the same IQR treatment applied to raw team points
    # rather than offensive rating -- distinct signal (a high-pace/low-ORtg
    # environment can still have a tight scoring spread, and vice versa).
    if len(pooled.team_points) >= MIN_LANDSCAPE_SAMPLE:
        values["team_points_iqr"] = _percentile(pooled.team_points, 0.75) - _percentile(
            pooled.team_points, 0.25
        )
    else:
        values["team_points_iqr"] = None
    values["top_decile_minutes_share"] = _top_decile_share(pooled.minutes_by_identity)
    values["top_decile_points_share"] = _top_decile_share(pooled.points_by_identity)
    return values


def _top_decile_share(by_identity: dict[int, float]) -> Optional[float]:
    """Share of a total held by the busiest ``ceil(10%)`` distinct participants."""
    totals = [v for v in by_identity.values() if v > 0]
    grand_total = sum(totals)
    if not totals or grand_total <= 0:
        return None
    top_n = math.ceil(0.10 * len(totals))
    top_sum = sum(sorted(totals, reverse=True)[:top_n])
    return top_sum / grand_total


@dataclass
class _FieldComposition:
    """Resolved counts + per-attribute known/unknown/total for a scope."""

    appeared_players: int
    appeared_unresolved: int
    rookie_count: int
    returner_count: int
    drafted_count: int
    undrafted_count: int
    not_yet_drafted_count: int
    first_round_count: int
    second_round_count: int
    lottery_count: int
    median_age: Optional[float]
    repeat_participants: Optional[int]
    attributes: dict[str, dict[str, Any]]


# Appearance-rank bucket order (contract §5: rank 1 is first-time).
_APPEARANCE_BUCKET_ORDER: tuple[str, ...] = ("1", "2", "3", "4+")


def _appearance_bucket(rank: int) -> str:
    """Map a distinct-calendar-year appearance rank to its display bucket."""
    return str(rank) if rank <= 3 else "4+"


def _ordered_buckets(
    distribution: dict[str, int], order: tuple[str, ...]
) -> Optional[dict[str, int]]:
    """Rebuild ``distribution`` in a fixed display order.

    Keys not in ``order`` are appended afterward (defensive; every producer
    here only emits keys in ``order``). Returns ``None`` when empty, never an
    empty dict.
    """
    if not distribution:
        return None
    ordered = {k: distribution[k] for k in order if k in distribution}
    for k, v in distribution.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def _year_buckets(distribution: dict[str, int]) -> Optional[dict[str, int]]:
    """Draft-class year buckets sorted ascending, with 'unknown' trailing."""
    if not distribution:
        return None
    return dict(
        sorted(distribution.items(), key=lambda kv: (kv[0] == "unknown", kv[0]))
    )


def _field_composition(
    pooled: _PooledScope, attributes: dict[int, _PlayerAttributes]
) -> _FieldComposition:
    """Derive event-time field composition over resolved appeared players.

    Draft status is evaluated at event time (``draft_year <= profile year``);
    age is the event-time age at the competition start (or a player's first
    eligible appearance date for a season scope, falling back to July 1). Unknown
    attributes stay visible — they are never dropped from a denominator silently.

    Beyond the base draft/age/position/origin attributes, this also derives:

    * ``draft_class`` — the player's draft-year cohort (independent of
      event-time drafted/undrafted/not-yet status).
    * ``appearance`` — the distinct-calendar-year appearance-rank distribution
      (contract §5), a finer-grained sibling to the rookie/returner binary.
    * ``age_reference`` / ``position_source`` — disclosure of *which source*
      resolved the base ``age`` / ``position`` value: known = the preferred
      event-time source; unknown = a documented fallback was used (July 1 for
      age, canonical ``player_status`` position for position). These are
      distinct from whether the base attribute itself is known at all.
    """
    year = pooled.scope.year
    resolved = sorted(pooled.resolved_player_ids)
    appeared = len(resolved)

    rookie = returner = 0
    drafted = undrafted = not_yet = draft_unknown = 0
    first_round = second_round = lottery = 0
    draft_distribution: dict[str, int] = defaultdict(int)
    draft_class_distribution: dict[str, int] = defaultdict(int)
    draft_class_known = draft_class_unknown = 0
    position_distribution: dict[str, int] = defaultdict(int)
    position_known = 0
    position_event_time = position_fallback = 0
    ages: list[float] = []
    age_reference_known = age_reference_fallback = 0
    appearance_distribution: dict[str, int] = defaultdict(int)
    appearance_known = appearance_unknown = 0

    for player_id in resolved:
        attr = attributes.get(player_id, _PlayerAttributes())

        # First-time vs returner (calendar-year appearance rank).
        if attr.first_sl_year is not None and attr.first_sl_year < year:
            returner += 1
        else:
            rookie += 1

        # Appearance-number distribution: the finer-grained rank behind the
        # rookie/returner binary above -- how many distinct SL calendar years
        # (across every competition) the player has reached, up to and
        # including this profile's year.
        rank = sum(1 for y in attr.sl_years if y <= year) if attr.sl_years else 0
        if rank > 0:
            appearance_distribution[_appearance_bucket(rank)] += 1
            appearance_known += 1
        else:
            appearance_unknown += 1

        # Draft status at event time.
        if attr.draft_year is None:
            # No draft record: treated as undrafted, per the app's draft-filter
            # convention (draft columns are absent for undrafted players).
            undrafted += 1
            draft_distribution["undrafted"] += 1
        elif attr.draft_year > year:
            not_yet += 1
            draft_distribution["not_yet_drafted"] += 1
        elif attr.draft_round in (1, 2):
            drafted += 1
            draft_distribution["drafted"] += 1
            if attr.draft_round == 1:
                first_round += 1
                if attr.draft_pick is not None and attr.draft_pick <= 14:
                    lottery += 1
            else:
                second_round += 1
        else:
            draft_unknown += 1
            draft_distribution["unknown"] += 1

        # Draft-class: the player's draft-year cohort, independent of the
        # event-time drafted/undrafted/not-yet status computed above.
        if attr.draft_year is not None:
            draft_class_distribution[str(attr.draft_year)] += 1
            draft_class_known += 1
        else:
            draft_class_unknown += 1

        # Position: event-time first, canonical fallback -- disclosed which
        # source resolved it (contract §5: "current canonical position is a
        # labeled fallback").
        event_position = pooled.event_position_by_player.get(player_id)
        position = event_position or attr.canonical_position
        if position:
            position_known += 1
            position_distribution[str(position)] += 1
            if event_position:
                position_event_time += 1
            else:
                position_fallback += 1

        # Age at event time -- disclosed whether the July 1 fallback was used.
        if attr.birthdate is not None:
            reference, used_fallback = _age_reference_info(pooled, player_id)
            ages.append(_age_years(attr.birthdate, reference))
            if used_fallback:
                age_reference_fallback += 1
            else:
                age_reference_known += 1

    median_age = round(_percentile(ages, 0.5), 1) if ages else None
    age_known = len(ages)

    # Repeat participants: canonical players appearing in more than one member
    # competition within a season profile (contract §5). Not applicable to a
    # single-competition scope, so it is disclosed as None rather than 0.
    repeat_participants: Optional[int] = None
    if pooled.scope.scope_kind == "season_all_competitions":
        member_counts: Counter[int] = Counter()
        for member in pooled.members:
            member_counts.update(member.resolved_player_ids)
        repeat_participants = sum(1 for c in member_counts.values() if c > 1)

    attributes_out: dict[str, dict[str, Any]] = {
        "draft": {
            "known": drafted + undrafted + not_yet,
            "unknown": draft_unknown,
            "total": appeared,
            "distribution": dict(draft_distribution) or None,
            "reason": None,
        },
        "draft_class": {
            "known": draft_class_known,
            "unknown": draft_class_unknown,
            "total": appeared,
            "distribution": _year_buckets(dict(draft_class_distribution)),
            "reason": None,
        },
        "age": {
            "known": age_known,
            "unknown": appeared - age_known,
            "total": appeared,
            "distribution": None,
            "reason": None,
        },
        "age_reference": {
            "known": age_reference_known,
            "unknown": age_reference_fallback,
            "total": age_known,
            "distribution": None,
            "reason": (
                "known = age computed at the exact competition/appearance "
                "date; unknown = July 1 fallback used because the event date "
                "was unavailable."
            ),
        },
        "position": {
            "known": position_known,
            "unknown": appeared - position_known,
            "total": appeared,
            "distribution": dict(position_distribution) or None,
            "reason": None,
        },
        "position_source": {
            "known": position_event_time,
            "unknown": position_fallback,
            "total": position_known,
            "distribution": None,
            "reason": (
                "known = event-time roster/starter position; unknown = "
                "canonical player_status position used as a labeled fallback."
            ),
        },
        "appearance": {
            "known": appearance_known,
            "unknown": appearance_unknown,
            "total": appeared,
            "distribution": _ordered_buckets(
                dict(appearance_distribution), _APPEARANCE_BUCKET_ORDER
            ),
            "reason": None,
        },
        # Origin (pre-event college/international affiliation) has insufficient
        # provenance for v1; disclosed as fully unknown rather than inferred from
        # current biography text (contract §5 / coverage audit).
        "origin": {
            "known": 0,
            "unknown": appeared,
            "total": appeared,
            "distribution": None,
            "reason": (
                "Pre-event college/international affiliation provenance is "
                "not yet sufficient to certify this distribution in v1; "
                "disclosed as fully unavailable rather than inferred from "
                "current biography."
            ),
        },
    }

    return _FieldComposition(
        appeared_players=appeared,
        appeared_unresolved=len(pooled.unresolved_source_ids),
        rookie_count=rookie,
        returner_count=returner,
        drafted_count=drafted,
        undrafted_count=undrafted,
        not_yet_drafted_count=not_yet,
        first_round_count=first_round,
        second_round_count=second_round,
        lottery_count=lottery,
        median_age=median_age,
        repeat_participants=repeat_participants,
        attributes=attributes_out,
    )


def _age_reference_info(pooled: _PooledScope, player_id: int) -> tuple[date, bool]:
    """Event-time reference date for a player's age in a scope.

    Also reports whether the July 1 fallback was used (contract §5: "expose
    fallback coverage"). Competition scope uses the competition start; a
    season scope uses the player's first eligible appearance date that year.
    Either falls back to July 1 of the profile year when the date is absent.
    """
    year = pooled.scope.year
    if pooled.scope.scope_kind == "competition":
        member = pooled.members[0] if pooled.members else None
        if member is not None and member.starts_on is not None:
            return member.starts_on, False
    else:
        appearance = pooled.first_date_by_player.get(player_id)
        if appearance is not None:
            return appearance, False
    return date(year, *FALLBACK_MONTH_DAY), True


# ---------------------------------------------------------------------------
# Candidate assembly, validation, and atomic publication.
# ---------------------------------------------------------------------------


@dataclass
class _ScopeCandidate:
    """A fully computed, not-yet-persisted profile plus its child rows."""

    scope: EnvironmentScope
    profile: SummerLeagueEnvironmentProfile
    coverage: list[SummerLeagueEnvironmentMetricCoverage]
    composition_rows: list[SummerLeagueEnvironmentFieldComposition]
    provenance_rows: list[SummerLeagueEnvironmentProvenance]
    membership: list[SummerLeagueEnvironmentSeasonMembership]
    complete_metric_count: int


def _coverage_reason(
    source: CoverageSource, verdict: str, covered: int, eligible: int
) -> Optional[str]:
    """Human reason recorded beside a non-complete coverage verdict."""
    if verdict == COVERAGE_COMPLETE:
        return None
    label = source.value
    if verdict == COVERAGE_UNAVAILABLE:
        if eligible <= 0:
            return f"no eligible games for {label} coverage"
        return f"no game carried {label} input"
    return f"{covered} of {eligible} games carried {label} input"


def _build_candidate(
    pooled: _PooledScope, attributes: dict[int, _PlayerAttributes]
) -> _ScopeCandidate:
    """Compute a scope's metric values, coverage, and field composition."""
    env_values = _environment_metric_values(pooled)
    composition = _field_composition(pooled, attributes)
    scope = pooled.scope

    # Identity dates (contract: "competition start/end dates"). Competition
    # scope uses its one member's dates; a season scope spans the earliest
    # start / latest end among members that have a known date, so one missing
    # member date never blanks the whole window.
    if scope.scope_kind == "competition":
        member0 = pooled.members[0] if pooled.members else None
        starts_on = member0.starts_on if member0 is not None else None
        ends_on = member0.ends_on if member0 is not None else None
    else:
        member_starts = [m.starts_on for m in pooled.members if m.starts_on is not None]
        member_ends = [m.ends_on for m in pooled.members if m.ends_on is not None]
        starts_on = min(member_starts) if member_starts else None
        ends_on = max(member_ends) if member_ends else None

    # Per-metric coverage verdicts (disclosure for every registered metric).
    coverage_rows: list[SummerLeagueEnvironmentMetricCoverage] = []
    complete_count = 0
    for definition in METRIC_DEFINITIONS:
        verdict, covered, eligible = _coverage_for_source(
            pooled, definition.coverage_source
        )
        if verdict == COVERAGE_COMPLETE:
            complete_count += 1
        coverage_rows.append(
            SummerLeagueEnvironmentMetricCoverage(
                metric_key=definition.key,
                coverage=verdict,
                covered_games=covered,
                eligible_games=eligible,
                reason=_coverage_reason(
                    definition.coverage_source, verdict, covered, eligible
                ),
            )
        )

    # Typed stored metric values. Box/shot/score/OT metrics publish only when
    # their input coverage is complete (partial → NULL). Identity-sourced values
    # (median age) publish over the resolved denominator per contract §5.
    box_gated = {
        CoverageSource.BOX,
        CoverageSource.SHOT,
        CoverageSource.SCORE,
        CoverageSource.OT_STATE,
    }
    stored_values: dict[str, Optional[float]] = {}
    for definition in METRIC_DEFINITIONS:
        if not definition.stored or definition.section is MetricSection.COMPOSITION:
            continue
        verdict, _covered, _eligible = _coverage_for_source(
            pooled, definition.coverage_source
        )
        value = env_values.get(definition.key)
        if definition.coverage_source in box_gated and verdict != COVERAGE_COMPLETE:
            value = None
        stored_values[definition.key] = (
            None if value is None else round(value, definition.rounding)
        )

    profile = SummerLeagueEnvironmentProfile(
        scope_key=scope.scope_key,
        scope_kind=(
            SCOPE_KIND_COMPETITION
            if scope.scope_kind == "competition"
            else SCOPE_KIND_SEASON
        ),
        year=scope.year,
        competition_id=scope.competition_id,
        venue_slug=pooled.venue_slug if scope.scope_kind == "competition" else None,
        display_name=pooled.display_name,
        starts_on=starts_on,
        ends_on=ends_on,
        version=0,  # assigned at publication
        is_current=False,  # flipped at publication
        registry_version=REGISTRY_VERSION,
        calculation_version=CALCULATION_VERSION,
        included_competitions=max(1, len(pooled.members)),
        final_games=pooled.final_games,
        # The persisted column is the "Scheduled / not-final" disclosure
        # (contract §3): every non-final game, not just SCHEDULED status --
        # scheduled_games and other_games (in-progress/postponed/canceled/
        # unknown) are split at the per-competition level for finer-grained
        # future use but combine here so a live/postponed/canceled slate
        # never silently disappears from schedule/status counts.
        scheduled_games=pooled.scheduled_games + pooled.other_games,
        distinct_teams=len(pooled.team_entry_ids),
        box_complete_games=pooled.box_complete_games,
        shot_covered_games=pooled.shot_covered_games,
        pbp_covered_games=pooled.pbp_covered_games,
        games_with_score=pooled.games_with_score,
        games_with_known_ot=pooled.games_with_known_ot,
        appeared_players=composition.appeared_players,
        appeared_unresolved=composition.appeared_unresolved,
        participation_count=pooled.participation_count or None,
        player_games=pooled.player_game_rows,
        rookie_count=composition.rookie_count,
        returner_count=composition.returner_count,
        drafted_count=composition.drafted_count,
        undrafted_count=composition.undrafted_count,
        not_yet_drafted_count=composition.not_yet_drafted_count,
        first_round_count=composition.first_round_count,
        second_round_count=composition.second_round_count,
        lottery_count=composition.lottery_count,
        teams_represented=len(pooled.team_entry_ids),
        median_age=composition.median_age,
        repeat_participants=composition.repeat_participants,
        source_watermark=pooled.watermark,
        raw_run_ids=sorted(pooled.raw_run_ids) or None,
        **stored_values,  # type: ignore[arg-type]
    )

    composition_rows = [
        SummerLeagueEnvironmentFieldComposition(
            attribute_key=attribute_key,
            known=payload["known"],
            unknown=payload["unknown"],
            total=payload["total"],
            distribution=payload["distribution"],
            reason=payload.get("reason"),
        )
        for attribute_key, payload in composition.attributes.items()
    ]

    provenance_rows = [
        SummerLeagueEnvironmentProvenance(
            source_kind=source_kind,
            watermark_at=prov.watermark,
            row_count=prov.row_count,
            parse_status=pooled.parse_status_by_source.get(source_kind),
            source_status=pooled.raw_run_status,
        )
        for source_kind, prov in sorted(pooled.provenance.items())
    ]

    membership: list[SummerLeagueEnvironmentSeasonMembership] = []
    if scope.scope_kind == "season_all_competitions":
        for member in pooled.members:
            membership.append(
                SummerLeagueEnvironmentSeasonMembership(
                    competition_id=member.competition_id,
                    year=member.year,
                    venue_slug=member.venue_slug,
                    final_games=member.final_games,
                )
            )

    return _ScopeCandidate(
        scope=scope,
        profile=profile,
        coverage=coverage_rows,
        composition_rows=composition_rows,
        provenance_rows=provenance_rows,
        membership=membership,
        complete_metric_count=complete_count,
    )


def _validate_candidate(candidate: _ScopeCandidate) -> None:
    """Assert the candidate obeys the frozen honesty invariants before publish.

    Raises:
        ValueError: when a box/shot/score/OT metric carries a value under
            non-complete coverage, or counts contradict each other.
    """
    profile = candidate.profile
    verdict_by_key = {row.metric_key: row.coverage for row in candidate.coverage}
    box_gated = {
        CoverageSource.BOX,
        CoverageSource.SHOT,
        CoverageSource.SCORE,
        CoverageSource.OT_STATE,
    }
    for definition in METRIC_DEFINITIONS:
        if not definition.stored or definition.section is MetricSection.COMPOSITION:
            continue
        if definition.coverage_source not in box_gated:
            continue
        value = getattr(profile, definition.key)
        if value is not None and verdict_by_key[definition.key] != COVERAGE_COMPLETE:
            raise ValueError(
                f"{candidate.scope.scope_key}: {definition.key} published under "
                f"{verdict_by_key[definition.key]} coverage"
            )
    if profile.box_complete_games > profile.final_games:
        raise ValueError(
            f"{candidate.scope.scope_key}: box-complete exceeds final games"
        )
    if profile.appeared_players < 0 or profile.appeared_unresolved < 0:
        raise ValueError(f"{candidate.scope.scope_key}: negative appeared-player count")
    if profile.not_yet_drafted_count < 0:
        raise ValueError(f"{candidate.scope.scope_key}: negative not-yet-drafted count")
    if profile.repeat_participants is not None and profile.repeat_participants < 0:
        raise ValueError(
            f"{candidate.scope.scope_key}: negative repeat-participant count"
        )


async def _publish_candidate(db: AsyncSession, candidate: _ScopeCandidate) -> int:
    """Insert the validated version and atomically flip ``is_current``.

    The previous current row (if any) is demoted in the same transaction so the
    partial-unique ``is_current`` index never sees two current rows, and both the
    demotion and the new current row commit together (contract §8). Returns the
    assigned version number.
    """
    scope_key = candidate.scope.scope_key
    existing = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueEnvironmentProfile.id,
                SummerLeagueEnvironmentProfile.version,
                SummerLeagueEnvironmentProfile.is_current,
            ).where(col(SummerLeagueEnvironmentProfile.scope_key) == scope_key)
        )
    ).all()
    max_version = max((int(v) for _id, v, _cur in existing), default=0)
    for row_id, _version, is_current in existing:
        if is_current:
            current = await db.get(SummerLeagueEnvironmentProfile, int(row_id))
            if current is not None:
                current.is_current = False
    # Flush the demotion before inserting the new current row so the unique
    # partial index is satisfied within the same transaction.
    await db.flush()

    profile = candidate.profile
    profile.version = max_version + 1
    profile.is_current = True
    # Stored profile timestamps are naive UTC to match the schema's
    # ``TIMESTAMP WITHOUT TIME ZONE`` columns (and the naive source watermarks).
    now = datetime.utcnow()
    profile.calculated_at = now
    profile.created_at = now
    profile.updated_at = now
    db.add(profile)
    await db.flush()
    assert profile.id is not None

    children: list[Any] = [
        *candidate.coverage,
        *candidate.composition_rows,
        *candidate.provenance_rows,
        *candidate.membership,
    ]
    for child in children:
        child.profile_id = profile.id
        db.add(child)
    await db.flush()
    return profile.version


@dataclass
class EnvironmentRebuildResult:
    """Structured summary of a rebuild for operators and tests (contract §8)."""

    requested_scopes: int = 0
    built_scopes: int = 0
    skipped_scopes: int = 0
    failed_scopes: int = 0
    metric_coverage_complete: int = 0
    registry_version: str = REGISTRY_VERSION
    calculation_version: str = CALCULATION_VERSION
    input_watermark: Optional[datetime] = None
    duration_seconds: float = 0.0
    published_scope_keys: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def _plan_scopes(
    inputs: dict[int, _CompetitionInputs],
    *,
    year: Optional[int],
    competition_id: Optional[int],
) -> list[_PooledScope]:
    """Build the pooled scopes to publish for a rebuild request.

    * ``competition_id`` → exactly that competition scope.
    * ``year`` → the all-competitions season scope plus every competition scope
      in that year.
    * neither → every competition scope and every year's season scope.
    """
    scopes: list[_PooledScope] = []

    def _competition_scope(member: _CompetitionInputs) -> _PooledScope:
        pooled = _PooledScope(
            scope=EnvironmentScope.for_competition(member.competition_id, member.year),
            display_name=member.display_name,
            venue_slug=member.venue_slug,
            members=[member],
        )
        pooled.pool()
        return pooled

    if competition_id is not None:
        member = inputs.get(competition_id)
        if member is not None:
            scopes.append(_competition_scope(member))
        return scopes

    members_by_year: dict[int, list[_CompetitionInputs]] = defaultdict(list)
    for member in inputs.values():
        members_by_year[member.year].append(member)
        scopes.append(_competition_scope(member))

    for scope_year, members in members_by_year.items():
        ordered = sorted(members, key=lambda m: m.competition_id)
        pooled = _PooledScope(
            scope=EnvironmentScope.for_season(scope_year),
            display_name=f"{scope_year} Summer League (All Competitions)",
            venue_slug=None,
            members=ordered,
        )
        pooled.pool()
        scopes.append(pooled)
    return scopes


async def rebuild_environment_profiles(
    db: AsyncSession,
    *,
    year: Optional[int] = None,
    competition_id: Optional[int] = None,
) -> EnvironmentRebuildResult:
    """Rebuild Competition Context profiles deterministically and publish atomically.

    Acquires the transaction-scoped Summer League writer lock **as its first
    action — before any source read** — then loads every eligible-final-game fact
    set-based, computes each scope's pooled metrics / coverage / field
    composition, validates the candidates, and publishes each valid version with
    one atomic ``is_current`` switch. It never commits or releases the lock
    itself: the caller owns the surrounding transaction, so a standalone rebuild
    wraps this in ``async with db.begin():`` and the locked pipeline
    materialization phase reuses its own already-locked transaction (the advisory
    lock is re-entrant within one transaction). Raw Summer League facts are never
    mutated; a candidate that fails validation is skipped, leaving its prior
    current profile readable.

    Args:
        db: Async session whose transaction will publish the new versions.
        year: Rebuild this calendar year's season + competition scopes.
        competition_id: Rebuild exactly this competition scope (wins over ``year``).

    Returns:
        An :class:`EnvironmentRebuildResult` summarizing requested/built/skipped/
        failed scopes, complete-metric coverage, the input watermark, and any
        per-scope failure reasons.
    """
    started = datetime.now(timezone.utc)
    # (1) Lock BEFORE the first input read (contract §8): reading any source fact
    # before serializing could combine raw facts and derived metrics from
    # different writer snapshots even if the final row switch is atomic.
    await acquire_summer_league_writer_lock(db)

    competitions = await _target_competitions(
        db, year=year, competition_id=competition_id
    )
    inputs = await _load_competition_inputs(db, competitions)
    scopes = _plan_scopes(inputs, year=year, competition_id=competition_id)

    all_player_ids: set[int] = set()
    for pooled in scopes:
        all_player_ids |= pooled.resolved_player_ids
    attributes = await _load_player_attributes(db, all_player_ids)

    result = EnvironmentRebuildResult(requested_scopes=len(scopes))
    for pooled in scopes:
        try:
            candidate = _build_candidate(pooled, attributes)
            _validate_candidate(candidate)
            # A SAVEPOINT around the one call that writes: if publication
            # raises a genuine DB error (constraint violation, etc.), rolling
            # back to the savepoint undoes only this scope's writes and
            # leaves the session usable for sibling scopes and the caller's
            # own failure handling -- an uncaught DB error here would
            # otherwise poison the whole outer transaction (shared with the
            # rest of the locked ingest pipeline phase), not just this scope.
            async with db.begin_nested():
                await _publish_candidate(db, candidate)
        except (ValueError, AssertionError, SQLAlchemyError) as exc:
            result.failed_scopes += 1
            result.failures[pooled.scope.scope_key] = str(exc)
            continue
        result.built_scopes += 1
        result.published_scope_keys.append(pooled.scope.scope_key)
        result.metric_coverage_complete += candidate.complete_metric_count
        result.input_watermark = _max_dt(result.input_watermark, pooled.watermark)

    result.skipped_scopes = (
        result.requested_scopes - result.built_scopes - result.failed_scopes
    )
    result.duration_seconds = (datetime.now(timezone.utc) - started).total_seconds()
    return result

"""Summer League adapter for the shared capability model (T8, #728).

:mod:`app.services.stats.capabilities` is source-agnostic on purpose -- it knows how to resolve
a metric's ``requires`` and test it against a ``provides`` set, but it has no idea what Summer
League's ``pbp_available`` / ``shotchart_available`` / ``adv_eligible`` flags mean (import
contract 3 forbids it from even importing this package). This module is the other half: it maps
Summer League's own availability flags -- the ones
:mod:`app.services.summer_league.normalization` already owns and sets -- onto the canonical
input vocabulary :mod:`app.services.stats.registry` entries declare their ``requires`` in, so
callers can ask "is metric X computable here" without re-deriving that mapping by hand at every
call site.

**Two shapes of "provides", for two shapes of caller.**

* :func:`pool_provides` builds a ``provides`` set from a competition's own availability flags --
  used where the caller already knows (or has fetched) those flags, e.g.
  :func:`app.services.summer_league.normalization.competition_capability_provides`.
* :func:`row_provides` / :func:`rows_provide` infer a ``provides`` set directly from a fetched
  row's populated fields, for call sites (the Explorer's player-value and rollup helpers) that
  only have query result rows in hand, not a competition object -- a PBP-derived field is
  ``None`` on a row exactly when the competition it came from never had PBP normalized (see
  :func:`app.services.summer_league.normalization.normalize_pbp_events`), so the row's own field
  presence is already the accurate per-row signal, no extra lookup required. Both shapes must
  agree in what they report for the same underlying data -- that agreement is what a source
  declaring box-only inputs really means.

Every box line always provides the raw box/rate inputs -- the Summer League ingest pipeline
never persists a season/game row without a box score. Pool-level team/opponent totals are also
available to box-derived player rates before the stricter ``adv_eligible`` threshold; only PBP,
shotchart, and pool-relative context tokens are conditional on the flags.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.services.stats.inputs import BOX_INT_FIELDS

# Every box line the Summer League pipeline persists carries these -- the raw counting-stat
# fields plus minutes, matching app.services.stats.inputs.StatInputs. This is the floor every
# Summer League source/pool provides, regardless of PBP/shotchart/adv_eligible.
BOX_PROVIDES: frozenset[str] = frozenset({*BOX_INT_FIELDS, "mp"})

# PBP-derived assist-attribution counts (app.services.stats.registry's astd_pct is the only
# registered consumer today). Populated only for games normalize_pbp_events has parsed --
# see app.services.summer_league.normalization.normalize_pbp_events.
PBP_PROVIDES: frozenset[str] = frozenset({"ast_fgm", "unast_fgm"})

# Shot-location data (normalize_shot_events). No registered metric consumes this yet; declaring
# the token now means a future shot-location metric becomes computable the moment a pool's
# shotchart_available flag is set, with no new gating code at its call site.
SHOTCHART_PROVIDES: frozenset[str] = frozenset({"shot_events"})

# Team/opponent totals are usable by box-derived player rates before a pool clears the
# stricter league-level threshold. Only the pool-relative recalibration context (VOP, DRB%,
# pace, and fitted BPM coefficients) is gated by ``adv_eligible``.
TEAM_OPPONENT_BOX_PROVIDES: frozenset[str] = frozenset({"team_box", "opponent_box"})
POOL_CONTEXT_PROVIDES: frozenset[str] = frozenset({"pool_context"})
ADV_CONTEXT_PROVIDES: frozenset[str] = (
    TEAM_OPPONENT_BOX_PROVIDES | POOL_CONTEXT_PROVIDES
)


def pool_provides(
    *,
    pbp_available: bool,
    shotchart_available: bool,
    adv_eligible: bool,
) -> frozenset[str]:
    """Canonical inputs a competition/pool provides, from its own availability flags.

    Args:
        pbp_available: ``SummerLeagueCompetition.pbp_available`` -- at least one game in this
            competition has parsed play-by-play (see
            :func:`app.services.summer_league.normalization.normalize_pbp_events`).
        shotchart_available: ``SummerLeagueCompetition.shotchart_available`` -- at least one
            game has parsed shot-location rows (see
            :func:`app.services.summer_league.normalization.normalize_shot_events`).
        adv_eligible: Whether this pool cleared the box-completeness threshold that makes
            team/opponent box totals and pool-relative recalibration (VOP, DRB%, pace, BPM fit)
            trustworthy -- ``SummerLeagueMetricContext.adv_eligible`` /
            ``SummerLeaguePlayerSeason.adv_eligible`` in
            :mod:`app.services.summer_league.metrics`. Not owned by ``normalization.py``, so
            callers that only have the competition row (not its metric context) pass whatever
            they already have; ``False`` is the conservative default.

    Returns:
        The frozenset of canonical input tokens this pool provides -- pass to
        :func:`app.services.stats.capabilities.is_computable` /
        :func:`~app.services.stats.capabilities.computable_metrics`.
    """
    provides = set(BOX_PROVIDES) | TEAM_OPPONENT_BOX_PROVIDES
    if pbp_available:
        provides |= PBP_PROVIDES
    if shotchart_available:
        provides |= SHOTCHART_PROVIDES
    if adv_eligible:
        provides |= POOL_CONTEXT_PROVIDES
    return frozenset(provides)


def _row_has_pbp_fields(r: Any) -> bool:
    """Whether a fetched row carries populated PBP assist-attribution counts.

    Duck-typed like :meth:`app.services.stats.inputs.StatInputs.add_row` -- ``r`` need only
    expose ``ast_fgm``/``unast_fgm`` by name (an ORM row, a query result tuple, a
    ``SimpleNamespace``). Both fields are written together or not at all (see
    ``app.services.summer_league.metrics._season_columns``), so either being non-``None`` is
    sufficient evidence, but both are checked defensively.
    """
    return (
        getattr(r, "ast_fgm", None) is not None
        or getattr(r, "unast_fgm", None) is not None
    )


def row_provides(r: Any) -> frozenset[str]:
    """Canonical inputs one fetched row provides, inferred from its populated fields.

    For call sites holding a single query-result row (not a competition object) -- e.g. the
    Explorer's per-row display helpers. See the module docstring for why row-level field
    presence is an accurate, extra-lookup-free stand-in for the competition's own
    ``pbp_available`` flag.
    """
    provides = set(BOX_PROVIDES)
    if _row_has_pbp_fields(r):
        provides |= PBP_PROVIDES
    return frozenset(provides)


def rows_provide(rows: Sequence[Any]) -> frozenset[str]:
    """Canonical inputs a set of rows collectively provide (union across rows).

    Used where several per-competition rows are being recombined into one career-grain value
    (e.g. :func:`app.services.summer_league_explorer_service.rollup_recombinable`): if *any* row
    in scope carries PBP counts, the recombined metric is computable (rows without PBP simply
    contribute nothing to that metric's numerator/denominator, the same way a box-only
    competition already contributes nothing today).
    """
    provides = set(BOX_PROVIDES)
    if any(_row_has_pbp_fields(r) for r in rows):
        provides |= PBP_PROVIDES
    return frozenset(provides)

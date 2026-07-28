"""How a Summer League metric fit becomes the active one.

Publication is deliberately its own module rather than a helper inside ``metrics.py``.
``metrics.py`` is a ~1,500-line god file and one of the named "complexity beyond a
reviewable unit" cases in the failure record, so new code goes beside it rather than
into it. The seam is real, not bookkeeping: everything here answers *how does a computed
result become the one readers see*, which is a different question from how it is computed.

It is also where the Phase 1 version-flip publish will live. That change generalizes
exactly this operation — build the new version outside any lock, then flip the current
pointer in a tiny transaction — from the model fit to the projection tables, so the
version-flip extends this module instead of growing ``metrics.py`` further.

See ``docs/plans/programmatic-code-discipline.md`` §1.4 and stat-engine §5.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import (
    DEFAULT_METRIC_CALCULATION_VERSION,
    DEFAULT_METRIC_REGISTRY_VERSION,
    SummerLeagueMetricContext,
    SummerLeagueMetricModel,
    SummerLeaguePlayerSeason,
)

# These are intentionally separate from the Competition Context registry versions.
# The player-season projection has its own formula and aggregation contract.
METRIC_REGISTRY_VERSION = DEFAULT_METRIC_REGISTRY_VERSION
METRIC_CALCULATION_VERSION = DEFAULT_METRIC_CALCULATION_VERSION

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from app.services.summer_league.metrics import ComputeResult

# Replacement level for the SL-native BPM/VORP fit. Imported lazily inside the function
# to avoid a circular import at module load: ``metrics`` imports this module.


async def publish_metric_model(
    db: AsyncSession,
    *,
    version: str,
    result: "ComputeResult",
    activate: bool = True,
) -> None:
    """Make ``version`` the active fit, retaining every prior fit as history.

    Replaces the full-table wipe the rebuild used to perform. The model table is the
    *auditable record of how the numbers were derived* — the Pythagorean exponent, the
    BPM regression and its R², the coefficient vector — and deleting it on every rebuild
    made each hour's fit unreproducible the moment the next hour ran. That was the one
    standing violation of P2 (retain history by default) in an otherwise
    longitudinal-first codebase; see ``docs/plans/north-star-architecture.md``.

    Retention is cheap: one row per unscoped rebuild, so hourly ticks add ~24 rows a day.

    Publishing is an upsert on ``model_version`` because that column is UNIQUE. Re-running
    a rebuild under a version that already exists refits that row in place rather than
    raising, which keeps a rebuild safely re-runnable — a Phase 1 exit criterion. Note the
    auto-minted version is second-granularity, so two rebuilds inside the same second are
    genuinely the same version and correctly collapse to one row.

    Args:
        db: Active session; the caller owns the transaction.
        version: Version stamp to publish as active.
        result: The freshly computed fit whose coefficients are being recorded.
        activate: Whether this fit should become active immediately. Staged
            rebuilds pass ``False`` and activate it with the projection pointer
            flip after snapshot materialization.
    """
    from app.services.summer_league.metrics import VORP_REPLACEMENT

    if activate:
        # Deactivate every prior fit first, so exactly one row is active at any instant
        # even if the publish below updates an existing row rather than inserting one.
        await db.execute(
            update(SummerLeagueMetricModel)
            .where(SummerLeagueMetricModel.is_active.is_(True))  # type: ignore[attr-defined]
            .values(is_active=False)
        )

    fit = {
        "pyth_exponent": result.pyth_exponent,
        "ws_ppw_coeff": result.ws_ppw_coeff,
        "pyth_n_teams": result.pyth_n,
        "bpm_intercept": result.bpm_intercept,
        "bpm_r2": result.bpm_r2,
        "bpm_n_fit": result.bpm_n_fit,
        "bpm_replacement": VORP_REPLACEMENT,
        "bpm_coefficients": result.bpm_coef or {},
    }

    existing = (
        await db.execute(
            select(SummerLeagueMetricModel).where(
                SummerLeagueMetricModel.model_version == version  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        db.add(
            SummerLeagueMetricModel(model_version=version, is_active=activate, **fit)
        )
        return

    for column, value in fit.items():
        setattr(existing, column, value)
    # Staging a colliding model version must not demote the current fit before
    # the projection pointer flip. The publication transaction handles that
    # demotion atomically with the candidate promotion.
    existing.is_active = activate or existing.is_active
    # Naive UTC to match the column's own ``default_factory=datetime.utcnow``.
    existing.fitted_at = datetime.now(timezone.utc).replace(tzinfo=None)


async def next_metric_version(db: AsyncSession) -> int:
    """Return the next publication version across both metric projections."""
    season_max = await db.scalar(select(func.max(SummerLeaguePlayerSeason.version)))
    context_max = await db.scalar(select(func.max(SummerLeagueMetricContext.version)))
    return max(int(season_max or 0), int(context_max or 0)) + 1


async def publish_metric_version(
    db: AsyncSession,
    *,
    version: int,
    competition_ids: set[int] | frozenset[int] | None = None,
    model_version: str | None = None,
) -> None:
    """Atomically expose one staged metric version to all readers.

    The caller owns a short transaction and, in production, the Summer League writer
    lock. Staged rows remain invisible until both projections have been demoted and the
    candidate rows promoted. A failed caller transaction therefore leaves the previous
    current version untouched.
    """
    season_scope = (
        SummerLeaguePlayerSeason.competition_id.in_(  # type: ignore[attr-defined]
            competition_ids
        )
        if competition_ids is not None
        else None
    )
    context_scope = (
        SummerLeagueMetricContext.competition_id.in_(  # type: ignore[attr-defined]
            competition_ids
        )
        if competition_ids is not None
        else None
    )

    season_demote = update(SummerLeaguePlayerSeason).where(
        SummerLeaguePlayerSeason.is_current.is_(True)  # type: ignore[attr-defined]
    )
    context_demote = update(SummerLeagueMetricContext).where(
        SummerLeagueMetricContext.is_current.is_(True)  # type: ignore[attr-defined]
    )
    if season_scope is not None:
        season_demote = season_demote.where(season_scope)
    if context_scope is not None:
        context_demote = context_demote.where(context_scope)

    await db.execute(season_demote.values(is_current=False))
    await db.execute(context_demote.values(is_current=False))
    # The partial unique indexes require the demotion to reach the database before the
    # candidate rows are promoted in the same transaction.
    await db.flush()

    season_promote = update(SummerLeaguePlayerSeason).where(
        SummerLeaguePlayerSeason.version == version  # type: ignore[arg-type]
    )
    context_promote = update(SummerLeagueMetricContext).where(
        SummerLeagueMetricContext.version == version  # type: ignore[arg-type]
    )
    if season_scope is not None:
        season_promote = season_promote.where(season_scope)
    if context_scope is not None:
        context_promote = context_promote.where(context_scope)
    await db.execute(season_promote.values(is_current=True))
    await db.execute(context_promote.values(is_current=True))

    # The global fit is staged inactive alongside a full rebuild. Scoped ticks reuse the
    # already-active fit and therefore do not touch this table.
    if competition_ids is None:
        await db.execute(
            update(SummerLeagueMetricModel)
            .where(SummerLeagueMetricModel.is_active.is_(True))  # type: ignore[attr-defined]
            .values(is_active=False)
        )
        if model_version is not None:
            await db.execute(
                update(SummerLeagueMetricModel)
                .where(
                    SummerLeagueMetricModel.model_version == model_version  # type: ignore[arg-type]
                )
                .values(is_active=True)
            )

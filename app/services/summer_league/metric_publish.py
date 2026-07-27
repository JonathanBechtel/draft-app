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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import SummerLeagueMetricModel

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from app.services.summer_league.metrics import ComputeResult

# Replacement level for the SL-native BPM/VORP fit. Imported lazily inside the function
# to avoid a circular import at module load: ``metrics`` imports this module.


async def publish_metric_model(
    db: AsyncSession, *, version: str, result: "ComputeResult"
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
    """
    from app.services.summer_league.metrics import VORP_REPLACEMENT

    # Deactivate every prior fit first, so exactly one row is active at any instant even
    # if the publish below updates an existing row rather than inserting one.
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
        db.add(SummerLeagueMetricModel(model_version=version, is_active=True, **fit))
        return

    for column, value in fit.items():
        setattr(existing, column, value)
    existing.is_active = True
    # Naive UTC to match the column's own ``default_factory=datetime.utcnow``.
    existing.fitted_at = datetime.now(timezone.utc).replace(tzinfo=None)

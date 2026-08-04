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

from datetime import date, datetime, timezone
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import (
    DEFAULT_METRIC_CALCULATION_VERSION,
    DEFAULT_METRIC_REGISTRY_VERSION,
    SummerLeagueMetricContext,
    SummerLeagueMetricModel,
    SummerLeagueDerivedAgg,
)
from app.services.sources.summer_league.metric_publish_guards import (
    assert_candidate_still_present,
    newer_current_competition_ids,
)

# These are intentionally separate from the Competition Context registry versions.
# The player-season projection has its own formula and aggregation contract.
METRIC_REGISTRY_VERSION = DEFAULT_METRIC_REGISTRY_VERSION
METRIC_CALCULATION_VERSION = DEFAULT_METRIC_CALCULATION_VERSION

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchivalPublication:
    """Counts written by a non-promoting archival publication."""

    contexts: int
    seasons: int

    @property
    def rows(self) -> int:
        """Return the total number of projection rows stamped published."""
        return self.contexts + self.seasons


if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from app.services.sources.summer_league.metrics import ComputeResult

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
    from app.services.sources.summer_league.metrics import VORP_REPLACEMENT

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
    """Return an atomic publication version, with a pre-migration fallback."""
    sequence_exists = await db.scalar(
        text("SELECT to_regclass('summer_league_metric_version_seq')")
    )
    if sequence_exists is not None:
        sequence_value = await db.scalar(
            text("SELECT nextval('summer_league_metric_version_seq')")
        )
        return int(sequence_value)

    # Test fixtures that create tables from SQLModel metadata do not run Alembic,
    # so retain a compatibility fallback for those DBs. Production databases have
    # the sequence from c5d6e7f8a9b0 and never use this race-prone legacy path.
    season_max = await db.scalar(select(func.max(SummerLeagueDerivedAgg.version)))
    context_max = await db.scalar(select(func.max(SummerLeagueMetricContext.version)))
    return max(int(season_max or 0), int(context_max or 0)) + 1


async def publish_metric_version(  # noqa: PLR0913
    db: AsyncSession,
    *,
    version: int,
    competition_ids: set[int] | frozenset[int] | None = None,
    model_version: str | None = None,
    as_of: datetime | None = None,
    effective_day: date | None = None,
) -> set[int]:
    """Atomically expose one staged metric version to all eligible readers.

    The caller owns a short transaction and, in production, the Summer League writer
    lock. Staged rows remain invisible until both projections have been demoted and the
    candidate rows promoted. If a newer version is already current for a competition,
    both projections leave that competition untouched while older scopes may still
    flip. Demoted rows retain their original ``published_at``; only newly promoted rows
    receive the flip timestamp. When supplied, ``as_of`` is also written to promoted
    rows so source currency is stamped at publication rather than inherited from an
    incomplete candidate. ``effective_day`` carries the Eastern event-calendar day
    from the rebuild input and never substitutes for ``as_of``. A failed caller
    transaction therefore leaves the previous current version untouched.

    Every scope about to be demoted is checked for candidate rows first, so a
    candidate that vanished after staging fails the publication loudly instead of
    emptying those scopes.

    Returns:
        Competition IDs whose newer current publication prevented this candidate
        from flipping that scope. Callers that materialize dependent read models
        should discard candidate-derived writes when this set is non-empty.

    Raises:
        MetricCandidateVanishedError: If a scope that would be demoted has no row
            staged at ``version``.
    """
    published_at = datetime.now(timezone.utc).replace(tzinfo=None)
    newer_competition_ids = await newer_current_competition_ids(
        db,
        version=version,
        competition_ids=competition_ids,
    )
    if newer_competition_ids:
        logger.info(
            "Skipping stale Summer League metric version %s for competitions "
            "already at newer versions: %s",
            version,
            sorted(newer_competition_ids),
        )

    # Runs before any demotion, inside the caller's locked publication
    # transaction, so compaction cannot remove the candidate between this check
    # and the flip below.
    await assert_candidate_still_present(
        db,
        version=version,
        competition_ids=competition_ids,
        skipped_competition_ids=newer_competition_ids,
    )

    season_scope: Any = (
        SummerLeagueDerivedAgg.competition_id.in_(  # type: ignore[attr-defined]
            competition_ids
        )
        if competition_ids is not None
        else None
    )
    context_scope: Any = (
        SummerLeagueMetricContext.competition_id.in_(  # type: ignore[attr-defined]
            competition_ids
        )
        if competition_ids is not None
        else None
    )

    season_demote = update(SummerLeagueDerivedAgg).where(
        SummerLeagueDerivedAgg.is_current.is_(True)  # type: ignore[attr-defined]
    )
    context_demote = update(SummerLeagueMetricContext).where(
        SummerLeagueMetricContext.is_current.is_(True)  # type: ignore[attr-defined]
    )
    if season_scope is not None:
        season_demote = season_demote.where(season_scope)
    if context_scope is not None:
        context_demote = context_demote.where(context_scope)
    if newer_competition_ids:
        season_demote = season_demote.where(
            SummerLeagueDerivedAgg.competition_id.not_in(newer_competition_ids)  # type: ignore[attr-defined]
        )
        context_demote = context_demote.where(
            SummerLeagueMetricContext.competition_id.not_in(newer_competition_ids)  # type: ignore[attr-defined]
        )

    await db.execute(season_demote.values(is_current=False))
    await db.execute(context_demote.values(is_current=False))
    # The partial unique indexes require the demotion to reach the database before the
    # candidate rows are promoted in the same transaction.
    await db.flush()

    season_promote = update(SummerLeagueDerivedAgg).where(
        SummerLeagueDerivedAgg.version == version  # type: ignore[arg-type]
    )
    context_promote = update(SummerLeagueMetricContext).where(
        SummerLeagueMetricContext.version == version  # type: ignore[arg-type]
    )
    if season_scope is not None:
        season_promote = season_promote.where(season_scope)
    if context_scope is not None:
        context_promote = context_promote.where(context_scope)
    if newer_competition_ids:
        season_promote = season_promote.where(
            SummerLeagueDerivedAgg.competition_id.not_in(newer_competition_ids)  # type: ignore[attr-defined]
        )
        context_promote = context_promote.where(
            SummerLeagueMetricContext.competition_id.not_in(newer_competition_ids)  # type: ignore[attr-defined]
        )
    promotion_values: dict[str, object] = {
        "is_current": True,
        "published_at": published_at,
    }
    promotion_values.update({"as_of": as_of} if as_of is not None else {})
    promotion_values.update(
        {"effective_day": effective_day} if effective_day is not None else {}
    )
    await db.execute(season_promote.values(**promotion_values))
    await db.execute(context_promote.values(**promotion_values))

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

    return newer_competition_ids


async def publish_archival_metric_version(  # noqa: PLR0913
    db: AsyncSession,
    *,
    version: int,
    competition_ids: set[int] | frozenset[int] | None = None,
    as_of: datetime | None = None,
    effective_day: date,
) -> ArchivalPublication:
    """Stamp an inactive candidate as a published historical daily close.

    This is intentionally a separate publication path from
    :func:`publish_metric_version`.  Archival rows are reader-visible only to the
    daily-trend query, which selects ``published_at`` rows by ``effective_day``;
    every normal reader filters ``is_current``.  The helper therefore updates only
    rows for the supplied candidate ``version`` that are already inactive and not
    yet published.  It never executes a demotion or promotion update, and refuses
    a malformed candidate that contains a current row.

    The caller owns the transaction and should hold the Summer League writer lock
    for the complete candidate-build + stamp sequence.  Re-running a version is a
    no-op because already-published rows are excluded by the update predicate.

    Args:
        db: Active async session; the caller controls commit/rollback.
        version: Candidate publication sequence to stamp.
        competition_ids: Optional competition scope. ``None`` means all scopes.
        as_of: Source-row watermark carried by the candidate.
        effective_day: Event calendar day (Eastern) for this archival close.

    Raises:
        ValueError: If the candidate contains a current row in the requested scope.
    """
    if competition_ids is not None and not competition_ids:
        return ArchivalPublication(contexts=0, seasons=0)

    season_scope = (
        SummerLeagueDerivedAgg.competition_id.in_(competition_ids)  # type: ignore[attr-defined]
        if competition_ids is not None
        else None
    )
    context_scope = (
        SummerLeagueMetricContext.competition_id.in_(competition_ids)  # type: ignore[attr-defined]
        if competition_ids is not None
        else None
    )

    # A current candidate is a publication-path violation.  Refusing it is safer
    # than silently stamping it: a future reader could otherwise observe a row that
    # archival code was never allowed to create.
    season_version: Any = getattr(SummerLeagueDerivedAgg, "version")
    season_current: Any = getattr(SummerLeagueDerivedAgg, "is_current")
    context_version: Any = getattr(SummerLeagueMetricContext, "version")
    context_current: Any = getattr(SummerLeagueMetricContext, "is_current")
    season_published_at: Any = getattr(SummerLeagueDerivedAgg, "published_at")
    context_published_at: Any = getattr(SummerLeagueMetricContext, "published_at")
    current_queries = [
        select(func.count())
        .select_from(SummerLeagueDerivedAgg)
        .where(
            season_version == version,
            season_current.is_(True),
        ),
        select(func.count())
        .select_from(SummerLeagueMetricContext)
        .where(
            context_version == version,
            context_current.is_(True),
        ),
    ]
    if season_scope is not None and context_scope is not None:
        current_queries[0] = current_queries[0].where(season_scope)
        current_queries[1] = current_queries[1].where(context_scope)
    current_rows = [int(await db.scalar(query) or 0) for query in current_queries]
    if any(current_rows):
        raise ValueError(
            "archival publication candidate contains current rows; refusing to "
            f"stamp version {version}"
        )

    published_at = datetime.now(timezone.utc).replace(tzinfo=None)
    season_update = update(SummerLeagueDerivedAgg).where(
        season_version == version,
        season_current.is_(False),
        season_published_at.is_(None),
    )
    context_update = update(SummerLeagueMetricContext).where(
        context_version == version,
        context_current.is_(False),
        context_published_at.is_(None),
    )
    if season_scope is not None and context_scope is not None:
        season_update = season_update.where(season_scope)
        context_update = context_update.where(context_scope)

    values: dict[str, object] = {
        "published_at": published_at,
        "effective_day": effective_day,
        "is_archival": True,
    }
    if as_of is not None:
        values["as_of"] = as_of

    season_result = await db.execute(season_update.values(**values))
    context_result = await db.execute(context_update.values(**values))
    await db.flush()
    return ArchivalPublication(
        contexts=int(getattr(context_result, "rowcount", 0) or 0),
        seasons=int(getattr(season_result, "rowcount", 0) or 0),
    )

"""Pre-flip safety checks for the Summer League metric pointer flip.

Publishing a metric version demotes every current row for the scopes it covers
and then promotes the candidate rows in the same transaction. Both halves are
unconditional updates, so the correctness of the flip rests entirely on what is
true *before* it runs:

* another publication may already have moved a scope past this candidate
  (:func:`newer_current_competition_ids`), and
* this candidate's own rows may no longer exist
  (:func:`assert_candidate_still_present`).

They live beside :mod:`app.services.sources.summer_league.metric_publish` rather than
inside it because they answer a different question -- *may this flip proceed,
and for which scopes* -- from the flip mechanics themselves, and because that
module is close to the file-size ratchet's threshold
(``docs/plans/programmatic-code-discipline.md`` §1.4).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueDerivedAgg,
)

# Both projections are keyed by competition and must reach the same per-scope
# verdict, so every check here iterates the pair.
PROJECTION_MODELS: tuple[Any, ...] = (
    SummerLeagueDerivedAgg,
    SummerLeagueMetricContext,
)


class MetricCandidateVanishedError(RuntimeError):
    """A staged candidate disappeared between staging and its pointer flip.

    Publishing demotes the rows readers currently see before promoting the
    candidate, so a candidate that no longer exists would leave a scope with no
    current row at all. Raising aborts the caller's short publication
    transaction, which rolls the demotion back and leaves the previous version
    live.
    """


async def newer_current_competition_ids(
    db: AsyncSession,
    *,
    version: int,
    competition_ids: set[int] | frozenset[int] | None,
) -> set[int]:
    """Return scopes already published at a version newer than the candidate.

    A full rebuild can finish after a scoped Desk tick has published a later
    version. The context and player-season projections must make the same
    per-competition decision, so the guard considers both tables.

    Args:
        db: Active session inside the caller's publication transaction.
        version: Candidate publication sequence about to be flipped.
        competition_ids: Optional scope; ``None`` means every competition.

    Returns:
        Competition IDs whose current version is newer than ``version``.
    """
    context_query = select(  # type: ignore[call-overload]
        SummerLeagueMetricContext.competition_id
    ).where(
        SummerLeagueMetricContext.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeagueMetricContext.version > version,  # type: ignore[operator]
    )
    season_query = select(  # type: ignore[call-overload]
        SummerLeagueDerivedAgg.competition_id
    ).where(
        SummerLeagueDerivedAgg.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeagueDerivedAgg.version > version,  # type: ignore[operator]
    )
    if competition_ids is not None:
        context_query = context_query.where(
            SummerLeagueMetricContext.competition_id.in_(competition_ids)  # type: ignore[attr-defined]
        )
        season_query = season_query.where(
            SummerLeagueDerivedAgg.competition_id.in_(competition_ids)  # type: ignore[attr-defined]
        )

    rows = (await db.execute(context_query.union(season_query))).scalars().all()
    return {int(competition_id) for competition_id in rows}


async def assert_candidate_still_present(
    db: AsyncSession,
    *,
    version: int,
    competition_ids: set[int] | frozenset[int] | None,
    skipped_competition_ids: set[int],
) -> None:
    """Refuse to demote a scope whose candidate rows are no longer there.

    Staging happens outside the writer lock and compaction runs as its own cron,
    so a candidate can be deleted between the two: an overlapping rebuild makes
    it rank 2 within the unpublished partition for its already-closed
    ``effective_day``. Demoting on behalf of rows that no longer exist promotes
    nothing and empties the scope, so check first and fail the transaction.

    Only scopes that would actually be demoted are considered. A scope with no
    current rows has nothing to lose (a first-ever publication is the normal
    case), and a scope skipped for a newer publication is left untouched anyway.

    Args:
        db: Active session inside the caller's publication transaction, which in
            production already holds the Summer League writer lock -- so
            compaction cannot delete the candidate between this check and the
            flip that follows it.
        version: Candidate publication sequence about to be flipped.
        competition_ids: Optional scope; ``None`` means every competition.
        skipped_competition_ids: Scopes excluded from the flip because a newer
            version is already current for them.

    Raises:
        MetricCandidateVanishedError: If a scope holding current rows has no row
            at ``version`` in either projection.
    """
    for model in PROJECTION_MODELS:
        competition_id: Any = model.competition_id
        is_current: Any = model.is_current
        row_version: Any = model.version
        query = (
            select(
                competition_id,
                func.bool_or(is_current).label("has_current"),
                func.bool_or(row_version == version).label("has_candidate"),
            )
            .where(or_(is_current.is_(True), row_version == version))
            .group_by(competition_id)
        )
        if competition_ids is not None:
            query = query.where(competition_id.in_(competition_ids))
        vanished = sorted(
            int(scope_id)
            for scope_id, has_current, has_candidate in (await db.execute(query)).all()
            if has_current
            and not has_candidate
            and int(scope_id) not in skipped_competition_ids
        )
        if vanished:
            raise MetricCandidateVanishedError(
                f"Summer League metric version {version} has no staged rows in "
                f"{model.__tablename__} for competitions {vanished}; refusing to "
                "demote their current rows. The candidate was most likely removed "
                "by version compaction after an overlapping rebuild staged a newer "
                "version. Re-run the rebuild to stage a fresh candidate."
            )

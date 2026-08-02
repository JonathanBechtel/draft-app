"""Backfill cumulative-through-day Summer League trend projection versions.

The operator job computes each historical close with the shared Summer League
metric engine, then stamps the inactive candidate through the archival publication
path.  A candidate build and its archival stamp share one bounded writer-lock
transaction, so a killed run leaves no reader-visible or idempotency-marker write.

Run (dev/staging first; never point this at production without the runbook):

  scripts/with-db-env.sh conda run -n draftguru python \
    scripts/backfill_sl_daily_trend_versions.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python \
    scripts/backfill_sl_daily_trend_versions.py --year 2024
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.metric_publish import (
    ArchivalPublication,
    publish_archival_metric_version,
)
from app.services.summer_league.metrics import (
    rebuild_staged,
)
from app.services.summer_league.write_lock import (
    acquire_summer_league_writer_lock_bounded,
)
from app.utils.db_async import SessionLocal, engine

MIN_BACKFILL_YEAR = 2017
MAX_BACKFILL_YEAR = 2026
DEFAULT_LOCK_MAX_WAIT_SECONDS = 30.0


@dataclass(frozen=True)
class BackfillTarget:
    """One competition/day cumulative close to compute."""

    competition_id: int
    year: int
    effective_day: date


@dataclass
class BackfillReport:
    """Measured operator counts suitable for dry-run and PR evidence."""

    planned: int = 0
    archived: int = 0
    skipped: int = 0
    contexts: int = 0
    seasons: int = 0


class _AlreadyArchived(Exception):
    """Internal sentinel used to roll back a raced duplicate target."""


def _valid_year(value: str) -> int:
    """Parse a historical backfill year and reject out-of-scope values."""
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid year: {value!r}") from exc
    if not MIN_BACKFILL_YEAR <= year <= MAX_BACKFILL_YEAR:
        raise argparse.ArgumentTypeError(
            f"year must be between {MIN_BACKFILL_YEAR} and {MAX_BACKFILL_YEAR}"
        )
    return year


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year",
        type=_valid_year,
        help=f"limit to one event year ({MIN_BACKFILL_YEAR}-{MAX_BACKFILL_YEAR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list measured targets without writing projections",
    )
    parser.add_argument(
        "--lock-max-wait-seconds",
        type=float,
        default=DEFAULT_LOCK_MAX_WAIT_SECONDS,
        help="bounded Summer League writer-lock wait (default: %(default)s)",
    )
    return parser


async def _load_targets(
    db: AsyncSession, *, year: int | None = None
) -> list[BackfillTarget]:
    """Return event days that have at least one resolved player game log."""
    competition_year: Any = getattr(SummerLeagueCompetition, "year")
    competition_id: Any = getattr(SummerLeagueCompetition, "id")
    game_date: Any = getattr(SummerLeagueGame, "game_date")
    player_id: Any = getattr(SummerLeaguePlayerGameLog, "player_id")
    minutes_seconds: Any = getattr(SummerLeaguePlayerGameLog, "minutes_seconds")
    query = (
        select(
            competition_id,
            competition_year,
            game_date,
        )
        .join(
            SummerLeagueGame,
            SummerLeagueGame.competition_id == SummerLeagueCompetition.id,
        )
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.game_id == SummerLeagueGame.id,
        )
        .where(
            competition_year.between(MIN_BACKFILL_YEAR, MAX_BACKFILL_YEAR),  # type: ignore[attr-defined]
            game_date.is_not(None),  # type: ignore[attr-defined]
            player_id.is_not(None),  # type: ignore[attr-defined]
            minutes_seconds > 0,  # type: ignore[operator]
        )
        .distinct()
        .order_by(
            SummerLeagueCompetition.year,
            SummerLeagueCompetition.id,
            SummerLeagueGame.game_date,
        )
    )
    if year is not None:
        query = query.where(competition_year == year)  # type: ignore[arg-type]
    rows = (await db.execute(query)).all()
    return [
        BackfillTarget(competition_id=int(cid), year=int(event_year), effective_day=day)
        for cid, event_year, day in rows
        if day is not None
    ]


async def _has_complete_archival_close(
    db: AsyncSession, *, competition_id: int, effective_day: date
) -> bool:
    """Return whether both projection families have a published daily close."""
    context_current: Any = getattr(SummerLeagueMetricContext, "is_current")
    context_published: Any = getattr(SummerLeagueMetricContext, "published_at")
    season_current: Any = getattr(SummerLeaguePlayerSeason, "is_current")
    season_published: Any = getattr(SummerLeaguePlayerSeason, "published_at")
    context_competition_id: Any = getattr(SummerLeagueMetricContext, "competition_id")
    context_effective_day: Any = getattr(SummerLeagueMetricContext, "effective_day")
    context_archival: Any = getattr(SummerLeagueMetricContext, "is_archival")
    season_competition_id: Any = getattr(SummerLeaguePlayerSeason, "competition_id")
    season_effective_day: Any = getattr(SummerLeaguePlayerSeason, "effective_day")
    season_archival: Any = getattr(SummerLeaguePlayerSeason, "is_archival")
    season_competition_bands: Any = getattr(
        SummerLeaguePlayerSeason, "trend_competition_bands"
    )
    season_year_bands: Any = getattr(SummerLeaguePlayerSeason, "trend_season_bands")
    context_count = await db.scalar(
        select(func.count())
        .select_from(SummerLeagueMetricContext)
        .where(
            context_competition_id == competition_id,
            context_effective_day == effective_day,
            context_current.is_(False),  # type: ignore[attr-defined]
            context_published.is_not(None),  # type: ignore[attr-defined]
            context_archival.is_(True),
        )
    )
    season_count = await db.scalar(
        select(func.count())
        .select_from(SummerLeaguePlayerSeason)
        .where(
            season_competition_id == competition_id,
            season_effective_day == effective_day,
            season_current.is_(False),  # type: ignore[attr-defined]
            season_published.is_not(None),  # type: ignore[attr-defined]
            season_archival.is_(True),
            season_competition_bands.is_not(None),
            season_year_bands.is_not(None),
        )
    )
    # A day with no resolved player rows is not a valid backfill target.  Requiring
    # both families prevents a partial marker from making a later retry a no-op.
    return bool(context_count and season_count)


async def _backfill_target(
    db: AsyncSession,
    target: BackfillTarget,
    *,
    lock_max_wait_seconds: float,
) -> ArchivalPublication:
    """Compute and archive one day atomically under the shared writer lock."""
    async with db.begin():
        # Keep the default READ COMMITTED isolation while polling. Once the lock
        # is acquired, the idempotency query gets a fresh statement snapshot and
        # can see a competing operator's just-committed archive.
        await acquire_summer_league_writer_lock_bounded(
            db, max_wait_seconds=lock_max_wait_seconds
        )
        # Recheck after acquiring the lock so two operators racing the same target
        # cannot both write a new archival version.
        if await _has_complete_archival_close(
            db,
            competition_id=target.competition_id,
            effective_day=target.effective_day,
        ):
            raise _AlreadyArchived
        summary = await rebuild_staged(
            db,
            model_version=(
                f"archive-{target.competition_id}-{target.effective_day.isoformat()}"
            ),
            competition_ids=[target.competition_id],
            effective_day=target.effective_day,
            through_day=target.effective_day,
        )
        if int(summary["contexts"]) == 0 or int(summary["seasons"]) == 0:
            # No projection families means no meaningful daily close.  Raising rolls
            # back the candidate rows and leaves the target retryable.
            raise RuntimeError(
                "shared metric engine produced no complete projection for "
                f"competition={target.competition_id} day={target.effective_day}"
            )
        return await publish_archival_metric_version(
            db,
            version=int(summary["version"]),
            competition_ids={target.competition_id},
            as_of=summary.get("as_of"),
            effective_day=target.effective_day,
        )


async def run_backfill(
    db: AsyncSession,
    *,
    year: int | None = None,
    dry_run: bool = False,
    lock_max_wait_seconds: float = DEFAULT_LOCK_MAX_WAIT_SECONDS,
) -> BackfillReport:
    """Run the historical backfill and return measured counts."""
    if lock_max_wait_seconds <= 0:
        raise ValueError("lock_max_wait_seconds must be positive")
    targets = await _load_targets(db, year=year)
    report = BackfillReport(planned=len(targets))
    if dry_run:
        for target in targets:
            print(
                f"DRY-RUN competition={target.competition_id} year={target.year} "
                f"effective_day={target.effective_day}"
            )
        return report

    # The target listing opens a read transaction on AsyncSession.  End it before
    # entering the per-target transaction so a failed target can roll back cleanly.
    await db.rollback()
    for target in targets:
        try:
            publication = await _backfill_target(
                db, target, lock_max_wait_seconds=lock_max_wait_seconds
            )
        except _AlreadyArchived:
            report.skipped += 1
            continue
        report.archived += 1
        report.contexts += publication.contexts
        report.seasons += publication.seasons
    return report


async def main(argv: Iterable[str] | None = None) -> None:
    """CLI entry point."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    async with SessionLocal() as db:
        report = await run_backfill(
            db,
            year=args.year,
            dry_run=args.dry_run,
            lock_max_wait_seconds=args.lock_max_wait_seconds,
        )
    print(
        f"SL daily trend backfill: planned={report.planned} "
        f"archived={report.archived} skipped={report.skipped} "
        f"contexts={report.contexts} seasons={report.seasons}"
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

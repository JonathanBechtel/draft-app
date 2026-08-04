"""Backfill cumulative-through-day Summer League trend projection versions.

The operator job computes each historical close with the shared Summer League
metric engine, then stamps the inactive candidate through the archival publication
path.  A candidate build and its archival stamp share one bounded writer-lock
transaction, so a killed run leaves no reader-visible or idempotency-marker write.

Each target is independent: a target that fails (no complete projection for the
day, a lock-acquisition timeout, a transient connection fault) is skipped and
reported rather than aborting the remaining targets, and the process exits
non-zero when any target failed.

Operational note -- run the full historical sweep off-season.  Every target
bootstraps a fresh through-day fit over the whole ``<= day`` source pool while
holding the Summer League writer lock, so a full 2017-present run occupies that
lock for a long stretch and would starve the Desk tick during a live event.  See
``docs/plans/summer-league-2027-preflight-runbook.md`` for the full operating
notes, including why trend modules render empty until this job has run.

Run (dev/staging first; never point this at production without the runbook):

  scripts/with-db-env.sh conda run -n draftguru python \
    scripts/backfill_sl_daily_trend_versions.py --dry-run
  scripts/with-db-env.sh conda run -n draftguru python \
    scripts/backfill_sl_daily_trend_versions.py --year 2024
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.metric_publish import (
    ArchivalPublication,
    publish_archival_metric_version,
)
from app.services.summer_league.metrics import (
    rebuild_staged,
    season_game_status_clause,
)
from app.services.summer_league.write_lock import (
    acquire_summer_league_writer_lock_bounded,
)
from app.utils.db_async import SessionLocal, engine

MIN_BACKFILL_YEAR = 2017
DEFAULT_LOCK_MAX_WAIT_SECONDS = 30.0
# One context row per competition/day close; the season count varies per target.
CONTEXT_ROWS_PER_TARGET = 1


def max_backfill_year(today: date | None = None) -> int:
    """Return the newest backfillable event year.

    The bound tracks the current Eastern calendar year rather than a hardcoded
    constant so that an in-event outage in a future season still has an archival
    repair tool. Scope such a repair with ``--year`` and read the runbook first:
    the sweep holds the Summer League writer lock.

    Args:
        today: Optional Eastern-calendar date, primarily for deterministic tests.

    Returns:
        The inclusive upper bound for ``--year`` and for target discovery.
    """
    current = today or to_eastern_date(datetime.now(timezone.utc))
    return max(MIN_BACKFILL_YEAR, current.year)


@dataclass(frozen=True)
class BackfillTarget:
    """One competition/day cumulative close to compute."""

    competition_id: int
    year: int
    effective_day: date


@dataclass(frozen=True)
class BackfillFailure:
    """One target that could not be archived, retained for the run summary."""

    target: BackfillTarget
    error: str


@dataclass
class BackfillReport:
    """Measured operator counts suitable for dry-run and PR evidence.

    In a real run ``contexts``/``seasons`` are the rows actually published. In a
    dry run they are estimates for the ``pending`` targets only, so the same
    numbers can be compared before and after.
    """

    planned: int = 0
    pending: int = 0
    archived: int = 0
    skipped: int = 0
    contexts: int = 0
    seasons: int = 0
    failures: list[BackfillFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Return the number of targets that raised and were skipped."""
        return len(self.failures)


class _AlreadyArchived(Exception):
    """Internal sentinel used to roll back a raced duplicate target."""


def _valid_year(value: str) -> int:
    """Parse a historical backfill year and reject out-of-scope values."""
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid year: {value!r}") from exc
    maximum = max_backfill_year()
    if not MIN_BACKFILL_YEAR <= year <= maximum:
        raise argparse.ArgumentTypeError(
            f"year must be between {MIN_BACKFILL_YEAR} and {maximum}"
        )
    return year


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year",
        type=_valid_year,
        help=f"limit to one event year ({MIN_BACKFILL_YEAR}-{max_backfill_year()})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "probe each target for an existing archival close and report the "
            "pending targets plus their estimated row counts, without writing"
        ),
    )
    parser.add_argument(
        "--lock-max-wait-seconds",
        type=float,
        default=DEFAULT_LOCK_MAX_WAIT_SECONDS,
        help="bounded Summer League writer-lock wait (default: %(default)s)",
    )
    return parser


def _eligible_day_clauses() -> tuple[Any, ...]:
    """Return the shared filter for days that can form a valid daily close."""
    competition_year: Any = getattr(SummerLeagueEdition, "year")
    game_date: Any = getattr(SummerLeagueGame, "game_date")
    player_id: Any = getattr(SummerLeaguePlayerGameLog, "player_id")
    minutes_seconds: Any = getattr(SummerLeaguePlayerGameLog, "minutes_seconds")
    return (
        competition_year.between(MIN_BACKFILL_YEAR, max_backfill_year()),
        game_date.is_not(None),
        player_id.is_not(None),
        minutes_seconds > 0,
        season_game_status_clause(),
    )


async def _load_targets(
    db: AsyncSession, *, year: int | None = None
) -> list[BackfillTarget]:
    """Return event days that have at least one resolved player game log."""
    competition_year: Any = getattr(SummerLeagueEdition, "year")
    competition_id: Any = getattr(SummerLeagueEdition, "id")
    game_date: Any = getattr(SummerLeagueGame, "game_date")
    query = (
        select(
            competition_id,
            competition_year,
            game_date,
        )
        .join(
            SummerLeagueGame,
            SummerLeagueGame.competition_id == SummerLeagueEdition.id,
        )
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.game_id == SummerLeagueGame.id,
        )
        .where(*_eligible_day_clauses())
        .distinct()
        .order_by(
            SummerLeagueEdition.year,
            SummerLeagueEdition.id,
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


def cumulative_player_counts(
    rows: Sequence[tuple[int, date, int]],
) -> dict[tuple[int, date], int]:
    """Return the cumulative distinct-player count for each competition/day.

    Each archival close is a through-day fit, so the season rows it publishes are
    the distinct players with resolved minutes on or before that day, not just
    that day's participants.

    Args:
        rows: ``(competition_id, game_day, player_id)`` triples in any order.

    Returns:
        Mapping of ``(competition_id, day)`` to the cumulative player count.
    """
    seen: dict[int, set[int]] = {}
    counts: dict[tuple[int, date], int] = {}
    for competition_id, day, player_id in sorted(
        rows, key=lambda row: (row[0], row[1])
    ):
        players = seen.setdefault(competition_id, set())
        players.add(player_id)
        counts[(competition_id, day)] = len(players)
    return counts


async def _load_cumulative_player_counts(
    db: AsyncSession, *, year: int | None = None
) -> dict[tuple[int, date], int]:
    """Load the per-target season-row estimate in one pass over eligible logs."""
    competition_year: Any = getattr(SummerLeagueEdition, "year")
    competition_id: Any = getattr(SummerLeagueEdition, "id")
    game_date: Any = getattr(SummerLeagueGame, "game_date")
    player_id: Any = getattr(SummerLeaguePlayerGameLog, "player_id")
    query = (
        select(competition_id, game_date, player_id)
        .join(
            SummerLeagueGame,
            SummerLeagueGame.competition_id == SummerLeagueEdition.id,
        )
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.game_id == SummerLeagueGame.id,
        )
        .where(*_eligible_day_clauses())
        .distinct()
    )
    if year is not None:
        query = query.where(competition_year == year)  # type: ignore[arg-type]
    rows = (await db.execute(query)).all()
    return cumulative_player_counts(
        [
            (int(cid), day, int(pid))
            for cid, day, pid in rows
            if day is not None and pid is not None
        ]
    )


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


async def _plan_dry_run(
    db: AsyncSession, targets: Sequence[BackfillTarget], *, year: int | None
) -> BackfillReport:
    """Probe every target and report pending work with estimated row counts."""
    report = BackfillReport(planned=len(targets))
    estimates = await _load_cumulative_player_counts(db, year=year)
    for target in targets:
        if await _has_complete_archival_close(
            db,
            competition_id=target.competition_id,
            effective_day=target.effective_day,
        ):
            report.skipped += 1
            print(
                f"DRY-RUN status=archived competition={target.competition_id} "
                f"year={target.year} effective_day={target.effective_day}"
            )
            continue
        seasons = estimates.get((target.competition_id, target.effective_day), 0)
        report.pending += 1
        report.contexts += CONTEXT_ROWS_PER_TARGET
        report.seasons += seasons
        print(
            f"DRY-RUN status=pending competition={target.competition_id} "
            f"year={target.year} effective_day={target.effective_day} "
            f"est_contexts={CONTEXT_ROWS_PER_TARGET} est_seasons={seasons}"
        )
    return report


async def run_backfill(
    db: AsyncSession,
    *,
    year: int | None = None,
    dry_run: bool = False,
    lock_max_wait_seconds: float = DEFAULT_LOCK_MAX_WAIT_SECONDS,
) -> BackfillReport:
    """Run the historical backfill and return measured counts.

    Targets are independent. A target that raises is recorded in
    ``BackfillReport.failures`` and the run continues, so one poison day cannot
    strand the rest of the sweep.

    Args:
        db: Active database session; the function owns its own transactions.
        year: Optional single event year to restrict the sweep to.
        dry_run: Probe targets and report estimates without writing.
        lock_max_wait_seconds: Bounded wait for the Summer League writer lock.

    Returns:
        Measured counts, including per-target failures.
    """
    if lock_max_wait_seconds <= 0:
        raise ValueError("lock_max_wait_seconds must be positive")
    targets = await _load_targets(db, year=year)
    if dry_run:
        return await _plan_dry_run(db, targets, year=year)

    report = BackfillReport(planned=len(targets))
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
        except Exception as exc:  # noqa: BLE001 - one poison target must not abort the sweep
            # ``db.begin()`` already rolled the failed target back; roll back again
            # defensively so a fault raised outside that scope cannot leave the
            # session mid-transaction for the next target.
            with contextlib.suppress(Exception):
                await db.rollback()
            report.failures.append(
                BackfillFailure(target=target, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        report.archived += 1
        report.contexts += publication.contexts
        report.seasons += publication.seasons
    return report


def format_report_lines(report: BackfillReport, *, dry_run: bool = False) -> list[str]:
    """Render the operator summary, including the per-target failure list."""
    label = "SL daily trend backfill" + (" (dry-run)" if dry_run else "")
    counts = "contexts" if not dry_run else "est_contexts"
    seasons = "seasons" if not dry_run else "est_seasons"
    lines = [
        f"{label}: planned={report.planned} pending={report.pending} "
        f"archived={report.archived} skipped={report.skipped} "
        f"failed={report.failed} {counts}={report.contexts} "
        f"{seasons}={report.seasons}"
    ]
    if report.failures:
        lines.append(f"FAILED TARGETS ({report.failed}):")
        lines.extend(
            f"  competition={failure.target.competition_id} "
            f"year={failure.target.year} "
            f"effective_day={failure.target.effective_day} "
            f"error={failure.error}"
            for failure in report.failures
        )
    return lines


async def main(argv: Iterable[str] | None = None) -> int:
    """CLI entry point; returns a non-zero status when any target failed."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        async with SessionLocal() as db:
            report = await run_backfill(
                db,
                year=args.year,
                dry_run=args.dry_run,
                lock_max_wait_seconds=args.lock_max_wait_seconds,
            )
    finally:
        # Always release the pool, including when target discovery itself raised.
        await engine.dispose()
    for line in format_report_lines(report, dry_run=args.dry_run):
        print(line)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

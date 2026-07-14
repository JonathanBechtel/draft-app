"""Standalone cron runner for scheduled Summer League game-data ingestion.

This script is designed to run as a scheduled Fly.io machine, executing an
incremental Summer League raw-fetch -> backbone -> advanced-metrics pipeline
directly against the database, without going through the HTTP API.

For each configured venue (NBA Stats LeagueID) it:

1. Refreshes the season index only (``leaguegamelog`` re-fetch, no per-game
   downloads) so newly scheduled/played games become visible.
2. Fetches any newly-appeared per-game files; already-downloaded games are
   skipped so this stays cheap on every run.
3. If the venue has zero games this run (pre-tip-off, or between events),
   logs and skips the backbone/normalization stages for that venue entirely
   -- ``backfill_summer_league_backbone`` raises when there are no raw
   manifests to audit, so this gate is required for safety.
4. Otherwise runs the audit -> normalize -> resolve backbone, then shot and
   play-by-play normalization, for that venue.

After every configured venue has been attempted, if *any* venue had games
this run, the global advanced-metrics table is rebuilt once (it is a full
wipe+rebuild, not scoped to one venue/year).

5. Refreshes the active Summer League event's *forward* schedule (the
   ``scheduleleaguev2`` feed, via
   ``app.services.summer_league.scoreboard_ingest.run_scoreboard_ingest``)
   so ``summer_league_games.tip_datetime`` stays fresh at this cron's own
   hourly cadence, decoupled from the Summer League Desk tick
   (``scripts/sl_desk_tick.py``) -- previously the *only* place that feed
   was ever polled, and only once the Desk itself was already non-dormant.
   That was a chicken-and-egg gap: the Desk can't wake up without tip
   times, and it can't get tip times because it's asleep. This step reuses
   the per-venue NBA Stats client already opened above (no second client)
   and is gated by a window guard (:func:`_schedule_pull_in_window`) so it
   never calls stats.nba.com during the long off-season -- see that
   function's docstring for exactly how the window is resolved. Additive
   and best-effort: a failure here is logged and does not fail the run, and
   the Desk tick's own step-0 scoreboard ingest is left untouched as
   belt-and-suspenders.

One venue failing does not abort the others. Fetch-stage errors (e.g.
stats.nba.com being unreachable) are treated the same as "no games yet" for
that venue and do not fail the run -- there is nothing to process either way.
Only a genuine failure *after* games were detected (backbone/normalization/
metrics errors) marks the run as failed. The schedule refresh (step 5) is
independently best-effort for the same reason -- see its own docstring.

Usage:
    python -m app.cli.summer_league_ingest_runner

Environment overrides:
    SL_INGEST_YEAR - four-digit Summer League year (default: 2026)
    SL_INGEST_LEAGUE_IDS - comma-separated NBA Stats LeagueIDs
        (default: "13,16,15")

Exit codes:
    0 - Success, including the pre-tip-off/no-games no-op case
    1 - Failure (check logs for details)
"""

import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventLifecyclePhase
from app.schemas.summer_league import SummerLeagueCompetition
from app.services.event_desk.lifecycle import lifecycle_phase
from app.services.event_desk.registry import DeskEvent, SUMMER_LEAGUE_REGISTRATION
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.backfill import (
    SummerLeagueBackfillOptions,
    backfill_summer_league_backbone,
    summarize_backfill_report,
)
from app.services.summer_league.endpoints import normalize_league_id
from app.services.summer_league.metrics import rebuild as rebuild_sl_metrics
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.normalization import (
    find_incomplete_team_box_game_ids,
    normalize_competition_games,
    normalize_pbp_events,
    normalize_player_game_logs,
    normalize_shot_events,
)
from app.services.summer_league.raw_ingestion import (
    RawIngestionOptions,
    SummerLeagueRawIngestor,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore
from app.services.summer_league.scoreboard_ingest import (
    ScoreboardIngestReport,
    resolve_target_competitions,
    run_scoreboard_ingest,
)
from app.services.summer_league.write_lock import (
    try_acquire_summer_league_writer_lock,
)
from app.utils.db_async import SessionLocal, dispose_engine

# Configure logging for cron context
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("summer_league_ingest_runner")

DEFAULT_YEAR = 2026
DEFAULT_LEAGUE_IDS = ("13", "16", "15")
RAW_ROOT = Path("data/raw/nba_stats/summer_league")

# Mirrors scripts/fetch_summer_league_raw.py defaults so cron traffic looks
# the same as a manual/local invocation to stats.nba.com.
FETCH_TIMEOUT_SECONDS = 30.0
FETCH_DELAY_SECONDS = 0.7
FETCH_RETRIES = 3
FETCH_RETRY_DELAY_SECONDS = 2.0


def _resolve_year() -> int:
    """Resolve the Summer League year, defaulting to the current season.

    Raises:
        ValueError: If ``SL_INGEST_YEAR`` is set but is not a plausible
            four-digit season year. Failing here (rather than deep inside a
            per-venue fetch, where ``normalize_season`` would raise and get
            swallowed as "no games") makes a misconfigured schedule fail
            loudly with a non-zero exit code instead of silently ingesting
            nothing for every venue.
    """
    raw = os.getenv("SL_INGEST_YEAR")
    if not raw or not raw.strip():
        return DEFAULT_YEAR
    stripped = raw.strip()
    try:
        year = int(stripped)
    except ValueError as exc:
        raise ValueError(
            f"SL_INGEST_YEAR must be a four-digit year, got {stripped!r}"
        ) from exc
    if not 1900 <= year <= 2100:
        raise ValueError(
            f"SL_INGEST_YEAR must be a four-digit year in [1900, 2100], got {year}"
        )
    return year


def _resolve_league_ids() -> list[str]:
    """Resolve the venue LeagueIDs to ingest from env, defaulting to all three."""
    raw = os.getenv("SL_INGEST_LEAGUE_IDS")
    values = raw.split(",") if raw and raw.strip() else list(DEFAULT_LEAGUE_IDS)

    league_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not stripped:
            continue
        league_id = normalize_league_id(stripped)
        if league_id in seen:
            continue
        seen.add(league_id)
        league_ids.append(league_id)

    if not league_ids:
        raise ValueError("No Summer League LeagueIDs resolved for ingestion")
    return league_ids


# Outer lifecycle phases worth an NBA Stats schedule-feed round-trip: the
# event is on the calendar (Announced), imminent (Warm-up), literally
# playing (Active), or in its short wind-down tail (a day or two past the
# last known game, per `post_roll_days` -- games can still slip/reschedule
# in that window). Dormant (nowhere near the window) and Archived (long
# over) are excluded so this hourly cron never calls stats.nba.com during
# the ~11 off-season months. Wider than `scripts/sl_desk_tick.py`'s own
# `_BOOTSTRAP_ELIGIBLE_PHASES` (which omits Wind-down) because that bootstrap
# only exists to escape dormancy -- once awake, the Desk tick's own step 0
# keeps polling every tick regardless of phase. This cron has no such
# always-runs-when-awake step of its own, so it must keep covering Wind-down
# itself to catch a late score/reschedule correction after the last game.
_SCHEDULE_ELIGIBLE_PHASES = frozenset(
    {
        EventLifecyclePhase.ANNOUNCED,
        EventLifecyclePhase.WARMUP,
        EventLifecyclePhase.ACTIVE,
        EventLifecyclePhase.WINDDOWN,
    }
)


def _synthetic_schedule_dates(
    competitions: Sequence[SummerLeagueCompetition],
) -> tuple[date, ...]:
    """Every day spanned by each target competition's ``starts_on``/``ends_on``.

    Mirrors ``scripts/sl_desk_tick.py``'s ``_synthetic_calendar_dates`` --
    duplicated here (not imported) since that module is a CLI entrypoint
    script, not a shared service, and this runner is itself a separate CLI
    entrypoint; both independently reuse the same two columns
    ``normalization.refresh_competition_date_window`` populates from real
    game data. Used as a stand-in for a real per-day ``summer_league_games``
    calendar so :func:`~app.services.event_desk.lifecycle.lifecycle_phase`'s
    gap-bridge clustering has something to reason about even on a run that
    hasn't ingested any games yet itself.

    Args:
        competitions: The target competitions for today
            (:func:`~app.services.summer_league.scoreboard_ingest.resolve_target_competitions`).

    Returns:
        Every date in each competition's inclusive ``[starts_on, ends_on]``
        span, possibly empty (and possibly containing duplicates across
        competitions -- `lifecycle_phase`'s clustering dedupes internally).
        A competition missing either date contributes nothing.
    """
    dates: list[date] = []
    for competition in competitions:
        if competition.starts_on is None or competition.ends_on is None:
            continue
        span_days = (competition.ends_on - competition.starts_on).days
        if span_days < 0:
            continue
        dates.extend(
            competition.starts_on + timedelta(days=offset)
            for offset in range(span_days + 1)
        )
    return tuple(dates)


async def _schedule_pull_in_window(db: AsyncSession, *, now: datetime) -> bool:
    """Window guard: is a registered Summer League competition in/near its window?

    Reuses the same pure outer-lifecycle state machine
    (:func:`~app.services.event_desk.lifecycle.lifecycle_phase`) the Summer
    League Desk itself uses, rather than hand-rolling date-range arithmetic,
    so this cron's notion of "in season" never drifts from the Desk's. Fed
    by each target competition's ``starts_on``/``ends_on`` window spread
    into a synthetic per-day calendar (:func:`_synthetic_schedule_dates`) --
    mirrors ``scripts/sl_desk_tick.py``'s ``_needs_scoreboard_bootstrap`` /
    `_synthetic_calendar_dates`` pattern for the identical "no
    ``summer_league_games`` rows yet to anchor the resolver" gap, since this
    guard runs *before* any schedule ingest this cron might do -- there may
    be no real per-day game dates yet to read.

    Uses :data:`~app.services.event_desk.registry.SUMMER_LEAGUE_REGISTRATION`'s
    static ``window_priors`` default rather than reading them back off the
    persisted ``events`` row (the only value ever written there, via
    ``EventRegistration.sync``) -- deliberately so this guard stays a pure
    read with no ``events`` upsert side effect just to decide there's
    nothing to do.

    A competition with zero games ever ingested *and* no
    ``starts_on``/``ends_on`` configured (a true first-ever cold start,
    before this cron -- or anything else -- has ever recorded a game date
    for it) has no signal to reason about and this returns ``False``. That
    mirrors the exact trade-off ``_needs_scoreboard_bootstrap`` already
    makes for the Desk tick: a from-scratch season's first game date has to
    get onto ``summer_league_games`` some other way (e.g. an operator's
    manual scoreboard-ingest run, or this cron's own per-venue raw
    ``leaguegamelog`` fetch once real games start appearing) before either
    guard can self-trigger the forward-schedule call.

    Args:
        db: Active database session.
        now: The run's reference instant (used both for the Eastern "today"
            competition-year fallback and as `lifecycle_phase`'s clock).

    Returns:
        Whether the caller should run :func:`~app.services.summer_league.scoreboard_ingest.run_scoreboard_ingest`
        this cycle.
    """
    today = to_eastern_date(now)
    competitions = await resolve_target_competitions(db, today=today)
    if not competitions:
        return False
    synthetic_dates = _synthetic_schedule_dates(competitions)
    if not synthetic_dates:
        return False
    desk_event = DeskEvent(
        key=SUMMER_LEAGUE_REGISTRATION.key,
        priority=SUMMER_LEAGUE_REGISTRATION.priority,
        window_priors=SUMMER_LEAGUE_REGISTRATION.window_priors,
        game_dates=synthetic_dates,
    )
    return lifecycle_phase(now, desk_event) in _SCHEDULE_ELIGIBLE_PHASES


async def _refresh_schedule(
    db: AsyncSession, *, now: datetime, client: NBAStatsClient
) -> ScoreboardIngestReport | None:
    """Step 5 -- refresh the active event's forward schedule, if in/near its window.

    Best-effort and additive: any failure (including an unexpected error
    inside :func:`_schedule_pull_in_window` itself) is logged and swallowed
    rather than failing the whole cron run -- this step exists to keep
    ``tip_datetime`` fresh for the (separate) Summer League Desk tick to
    read, not to gate this runner's own raw-ingestion/backbone/metrics
    responsibilities.

    Args:
        db: Active database session (caller controls the transaction).
        now: The run's reference instant.
        client: The NBA Stats client already opened by ``main`` for this
            run's per-venue raw fetches -- reused here rather than opening a
            second client.

    Returns:
        The :class:`~app.services.summer_league.scoreboard_ingest.ScoreboardIngestReport`
        when the window guard allowed a fetch; ``None`` when skipped
        (off-window) or on an unexpected failure.
    """
    try:
        in_window = await _schedule_pull_in_window(db, now=now)
        # `_schedule_pull_in_window`'s own read (`resolve_target_competitions`)
        # auto-begins a transaction on this session (SQLAlchemy async
        # "autobegin"); commit it here -- a no-op for the DB since nothing
        # was written -- before opening the explicit `db.begin()` below,
        # which would otherwise raise "a transaction is already begun".
        await db.commit()
        if not in_window:
            logger.info("Schedule refresh: skipped (off-window)")
            return None
        async with db.begin():
            if not await try_acquire_summer_league_writer_lock(db):
                logger.info("Schedule refresh: skipped (Desk writer is active)")
                return None
            report = await run_scoreboard_ingest(
                db, today=to_eastern_date(now), client=client
            )
        logger.info(
            "Schedule refresh: competitions_checked=%d games_seen=%d "
            "created=%d updated=%d errors=%s unresolved_team_ids=%s",
            report.competitions_checked,
            report.games_seen,
            report.games_created,
            report.games_updated,
            report.errors,
            report.unresolved_team_ids,
        )
        return report
    except Exception as exc:
        logger.warning(
            "Schedule refresh failed (%s: %s); continuing", type(exc).__name__, exc
        )
        # Leave the session usable for whatever `main` does next (the
        # metrics-rebuild step's own `db.begin()`): a mid-query failure can
        # leave an autobegun transaction needing a rollback, and `db.begin()`
        # raises (rather than no-ops) if one is still open.
        await db.rollback()
        return None


async def _run_venue(
    db: AsyncSession,
    ingestor: SummerLeagueRawIngestor,
    *,
    year: int,
    league_id: str,
) -> tuple[bool, bool]:
    """Incrementally fetch and process one Summer League venue.

    Args:
        db: Async database session shared across venues this run.
        ingestor: Raw NBA Stats ingestor (shared client/store across venues).
        year: Summer League season year.
        league_id: NBA Stats LeagueID for this venue.

    Returns:
        A ``(had_games, failed)`` tuple. ``had_games`` is True when at least
        one game was discovered for this venue this run. ``failed`` is True
        only for a genuine processing failure that happened *after* games
        were found -- fetch-stage errors are folded into ``had_games=False``
        since there is nothing to process either way.
    """
    try:
        # Step 1: refresh the season index only (limit_games=0 skips all
        # per-game downloads) so newly scheduled/played games become visible
        # without re-downloading every already-fetched game file.
        refresh_options = RawIngestionOptions(
            year=year,
            league_id=league_id,
            limit_games=0,
            force=True,
            delay_seconds=FETCH_DELAY_SECONDS,
        )
        refresh_manifest = ingestor.fetch_year_league(refresh_options)

        if not refresh_manifest.game_ids:
            logger.info("L%s: no games yet", league_id)
            return False, False

        # Step 2: fetch any newly-appeared games. Existing per-game files are
        # skipped (force=False), so only new games actually download.
        fetch_options = RawIngestionOptions(
            year=year,
            league_id=league_id,
            force=False,
            delay_seconds=FETCH_DELAY_SECONDS,
        )
        fetch_manifest = ingestor.fetch_year_league(fetch_options)
        logger.info(
            "L%s: %d games discovered (%d files written, %d skipped, %d errors)",
            league_id,
            fetch_manifest.game_count,
            len(fetch_manifest.files_written),
            len(fetch_manifest.files_skipped),
            len(fetch_manifest.errors),
        )
    except Exception as exc:
        logger.warning(
            "L%s: raw fetch failed (%s: %s); treating as no games yet this run",
            league_id,
            type(exc).__name__,
            exc,
        )
        return False, False

    try:
        async with db.begin():
            if not await try_acquire_summer_league_writer_lock(db):
                logger.info(
                    "L%s: skipping DB processing because the Desk writer is active",
                    league_id,
                )
                return False, False
            backfill_options = SummerLeagueBackfillOptions(
                year=year,
                league_id=league_id,
                raw_root=RAW_ROOT,
                create_stubs=True,
            )
            report = await backfill_summer_league_backbone(db, backfill_options)
            logger.info(
                "L%s backbone: %s", league_id, summarize_backfill_report(report)
            )

            shot_report = await normalize_shot_events(
                db, year=year, league_id=league_id, raw_root=RAW_ROOT
            )
            logger.info(
                "L%s shot events: %d upserted (%d/%d games with shots)",
                league_id,
                shot_report.shot_events_upserted,
                shot_report.games_with_shots,
                shot_report.games_processed,
            )

            pbp_report = await normalize_pbp_events(
                db, year=year, league_id=league_id, raw_root=RAW_ROOT
            )
            logger.info(
                "L%s PBP events: %d upserted (%d/%d games with PBP)",
                league_id,
                pbp_report.pbp_events_upserted,
                pbp_report.games_with_pbp,
                pbp_report.games_processed,
            )
    except Exception as exc:
        logger.error(
            "L%s backbone/normalization failed: %s", league_id, exc, exc_info=True
        )
        return True, True

    competition_id = (
        report.competition_games.competition_id if report.competition_games else None
    )
    if competition_id is not None:
        await _retry_incomplete_team_boxes(
            db, ingestor, year=year, league_id=league_id, competition_id=competition_id
        )

    return True, False


async def _retry_incomplete_team_boxes(
    db: AsyncSession,
    ingestor: SummerLeagueRawIngestor,
    *,
    year: int,
    league_id: str,
    competition_id: int,
) -> None:
    """Force-refetch and re-normalize any game still on the team-box fallback.

    The main fetch step above uses ``force=False``, so a per-game box-score
    file fetched moments too early -- the game just finished, NBA Stats
    hasn't posted the official box yet -- is cached forever and never
    revisited. Every run, this closes that gap: any game whose team row is
    still sourced from the season-gamelog fallback (see
    :func:`~app.services.summer_league.normalization.find_incomplete_team_box_game_ids`,
    which is how such a game is recognized -- it never carries team minutes)
    gets one fresh, forced re-fetch and re-normalize pass. Bounded to a
    single retry per run: a game still incomplete after the forced re-fetch
    stays that way until the next scheduled run rather than looping here.

    Network I/O runs with no transaction open (mirrors the main fetch step),
    so a slow or blocked NBA Stats response never leaves a DB transaction
    idle. The retry transaction re-normalizes only competition/team and player
    box rows; it deliberately does not rerun the full backbone's identity
    resolution and embedding work. Existing source-player mappings remain local
    to this database, and genuinely new unresolved identities are handled by the
    next normal hourly backbone pass.
    """
    async with db.begin():
        incomplete_ids = await find_incomplete_team_box_game_ids(
            db, competition_id=competition_id
        )
    if not incomplete_ids:
        return

    logger.info(
        "L%s: retrying %d game(s) still on the team-box fallback: %s",
        league_id,
        len(incomplete_ids),
        ", ".join(incomplete_ids),
    )
    retry_options = RawIngestionOptions(
        year=year,
        league_id=league_id,
        force=True,
        game_ids=tuple(incomplete_ids),
        skip_endpoints=("playbyplayv2", "shotchartdetail"),
        delay_seconds=FETCH_DELAY_SECONDS,
    )
    try:
        retry_manifest = ingestor.fetch_year_league(retry_options)
    except Exception as exc:
        logger.warning(
            "L%s: retry re-fetch failed (%s: %s); will retry again next run",
            league_id,
            type(exc).__name__,
            exc,
        )
        return
    if retry_manifest.errors:
        logger.warning(
            "L%s: %d error(s) re-fetching incomplete games; will retry again next run",
            league_id,
            len(retry_manifest.errors),
        )

    try:
        async with db.begin():
            competition_report = await normalize_competition_games(
                db,
                year=year,
                league_id=league_id,
                raw_root=RAW_ROOT,
            )
            player_report = await normalize_player_game_logs(
                db,
                year=year,
                league_id=league_id,
                raw_root=RAW_ROOT,
            )
            logger.info(
                "L%s box normalization (retry pass): %d team rows, "
                "%d player rows (%d skipped)",
                league_id,
                competition_report.team_game_logs_upserted,
                player_report.player_game_logs_upserted,
                player_report.player_game_logs_skipped,
            )
    except Exception as exc:
        logger.error(
            "L%s box re-normalize (retry pass) failed: %s",
            league_id,
            exc,
            exc_info=True,
        )


async def main() -> int:
    """Run the incremental Summer League ingestion cycle for all venues.

    Returns:
        Exit code (0 for success, including the pre-tip-off no-op case;
        1 for a genuine failure).
    """
    start_time = datetime.now(timezone.utc)
    client: NBAStatsClient | None = None

    try:
        try:
            year = _resolve_year()
            league_ids = _resolve_league_ids()
        except ValueError as exc:
            logger.error("Invalid Summer League ingest configuration: %s", exc)
            return 1

        logger.info(
            "Starting Summer League ingestion: year=%s league_ids=%s",
            year,
            ",".join(league_ids),
        )

        failed = False
        any_games = False

        client = NBAStatsClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            max_retries=FETCH_RETRIES,
            retry_delay_seconds=FETCH_RETRY_DELAY_SECONDS,
        )
        store = SummerLeagueRawStore(RAW_ROOT)
        ingestor = SummerLeagueRawIngestor(
            client=client,
            store=store,
            progress=lambda message: logger.info(message),
        )

        async with SessionLocal() as db:
            for league_id in league_ids:
                had_games, venue_failed = await _run_venue(
                    db, ingestor, year=year, league_id=league_id
                )
                any_games = any_games or had_games
                failed = failed or venue_failed

            # Step 5 -- forward-schedule refresh (decoupled from the Desk
            # tick, see module docstring). Independent of `any_games`: a
            # venue can be scoreless this run (pre-tip-off) while the
            # schedule feed still has fresh tip times to upsert. Reuses the
            # NBA Stats client already opened above; best-effort, so a
            # failure here never flips `failed`.
            await _refresh_schedule(db, now=start_time, client=client)

            if any_games:
                try:
                    async with db.begin():
                        if await try_acquire_summer_league_writer_lock(db):
                            summary = await rebuild_sl_metrics(db)
                            logger.info(
                                "SL metrics rebuild complete: %s player-seasons, "
                                "%s contexts (%s adv-eligible pools)",
                                summary["seasons"],
                                summary["contexts"],
                                summary["adv_pools"],
                            )
                        else:
                            logger.info(
                                "SL metrics rebuild skipped (Desk writer is active)"
                            )
                except Exception as exc:
                    failed = True
                    logger.error("SL metrics rebuild failed: %s", exc, exc_info=True)
            else:
                logger.info("No venue had games this run; skipping metrics rebuild")
    finally:
        if client is not None:
            client.close()
        await dispose_engine()

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    if failed:
        logger.error("Summer League ingestion finished with failures in %.1fs", elapsed)
        return 1

    logger.info("Summer League ingestion finished successfully in %.1fs", elapsed)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

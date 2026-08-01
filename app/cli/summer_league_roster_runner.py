"""Standalone cron runner for scheduled Summer League roster fetch + enrichment.

This script is designed to run as a scheduled Fly.io machine, porting the
brittle sprite ``launchd`` job (``scripts/poll_2026_satellite_rosters.sh``)
onto Fly. It executes the same fetch -> load -> resolve -> enrich pipeline
directly against the database, without going through the HTTP API.

For each configured venue (NBA.com LeagueID) it:

1. Resolves the shared Event Desk lifecycle window. Dormant and archived
   runs exit before opening the roster fetch path; the Announced, Warm-up,
   Active, and Wind-down phases remain eligible for polling.
2. Force-refreshes the roster snapshot from NBA.com (rosters are published
   close to each event, so a fresh fetch every run is required -- an empty
   snapshot written before publication is never refreshed otherwise).
3. If the venue has zero published players this run (pre-announcement, or
   the landing/team pages failed to fetch), logs and skips the rest of the
   pipeline for that venue entirely.
4. Otherwise loads the roster (idempotent upsert + diff), resolves source
   players to canonical players (creating stubs as needed), seeds NBA Stats
   external ids, backfills reference headshots, scrapes + ingests
   Basketball-Reference bios for changed or previously unsuccessful players,
   and backfills college stats for changed or still-missing players -- each
   stage committed independently so a downstream failure does not roll back
   an already-durable upstream stage.

One venue failing does not abort the others. A roster-fetch failure (e.g.
NBA.com being unreachable) is treated the same as "not published yet" for
that venue and does not fail the run -- there is nothing to load either way.
Only a genuine failure *after* a published roster was found (load/resolve/
enrichment errors) marks the run as failed.

Usage:
    python -m app.cli.summer_league_roster_runner

Environment overrides:
    SL_ROSTER_YEAR - four-digit Summer League year to scope this run to
        (default: the current Eastern calendar year). Setting this only
        narrows which year's competitions the lifecycle window check
        considers -- it does NOT bypass the window gate. For example,
        setting it to a year whose event has already ended still leaves
        the run dormant.
    SL_ROSTER_FORCE - set to "1" to bypass the lifecycle window gate
        entirely and run regardless of phase. This is the explicit,
        operator-directed backfill escape hatch; combine with
        SL_ROSTER_YEAR to target a specific season's data.
    SL_ROSTER_LEAGUE_IDS - comma-separated NBA.com LeagueIDs
        (default: "13,16,15")

Exit codes:
    0 - Success, including the not-yet-published no-op case
    1 - Failure (check logs for details)
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sys
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.college_stats_service import run_college_stats_sweep
from app.services.summer_league.bio_enrichment_targets import (
    select_bio_enrichment_targets,
)
from app.services.summer_league.endpoints import (
    SUPPORTED_SUMMER_LEAGUES,
    normalize_league_id,
)
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.event_window import is_summer_league_window_open
from app.services.player_bio.bbref_parse import PlayerBio
from app.services.player_bio.bbref_scrape import scrape_letters
from app.services.player_bio.ingest import ingest as ingest_player_bios_csv
from app.services.summer_league.headshots import backfill_nba_headshots
from app.services.summer_league.player_resolution import (
    backfill_nba_stats_external_ids,
    resolve_summer_league_players,
)
from app.services.summer_league.roster_fetch import RosterFetcher, RosterRunResult
from app.services.summer_league.roster_ingest import (
    CompetitionKey,
    load_roster_snapshot,
)
from app.services.summer_league.roster_changes import (
    canonical_player_ids,
    changed_source_player_ids,
)
from app.services.summer_league.roster_parse import RosterEntry
from app.utils.db_async import SessionLocal, dispose_engine, load_schema_modules

# Configure logging for cron context
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("summer_league_roster_runner")

DEFAULT_LEAGUE_IDS = ("13", "16", "15")
RAW_ROOT = Path("data/raw/nba_stats/summer_league")

# Mirrors the scripts/fetch_summer_league_rosters.py CLI defaults.
FETCH_TIMEOUT_SECONDS = 30.0
FETCH_DELAY_SECONDS = 0.5

# Mirrors the scripts/bbref_bio_scraper.py CLI defaults / output locations.
BIO_OUT_DIR = Path("data/scraper-output")
BIO_CACHE_DIR = Path("data/scraper-cache/players")
BIO_SCRAPE_THROTTLE_SECONDS = 3.0
BIO_SCRAPE_TIMEOUT_SECONDS = 30.0

_BIO_CSV_FIELDNAMES = [f.name for f in dataclass_fields(PlayerBio)]


def _default_year() -> int:
    """Return the current Eastern calendar year as the roster-poll default.

    Mirrors the today's-year fallback ``resolve_target_competitions`` already
    uses (``app/services/summer_league/scoreboard_ingest.py``) so the cron
    follows the calendar without a code change each season -- no more
    hard-coded season year to bump every summer.
    """
    return to_eastern_date(datetime.now(timezone.utc)).year


def _resolve_year() -> int:
    """Resolve the Summer League year, defaulting to the current season.

    Raises:
        ValueError: If ``SL_ROSTER_YEAR`` is set but is not a plausible
            four-digit season year. Failing here (rather than deep inside a
            per-venue fetch) makes a misconfigured schedule fail loudly with
            a non-zero exit code instead of silently fetching nothing for
            every venue.
    """
    raw = os.getenv("SL_ROSTER_YEAR")
    if not raw or not raw.strip():
        return _default_year()
    stripped = raw.strip()
    try:
        year = int(stripped)
    except ValueError as exc:
        raise ValueError(
            f"SL_ROSTER_YEAR must be a four-digit year, got {stripped!r}"
        ) from exc
    if not 1900 <= year <= 2100:
        raise ValueError(
            f"SL_ROSTER_YEAR must be a four-digit year in [1900, 2100], got {year}"
        )
    return year


def _resolve_league_ids() -> list[str]:
    """Resolve the venue LeagueIDs to poll from env, defaulting to all three."""
    raw = os.getenv("SL_ROSTER_LEAGUE_IDS")
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
        raise ValueError("No Summer League LeagueIDs resolved for roster polling")
    return league_ids


def _write_bio_csv(rows: list[dict[str, object]], out_path: Path) -> None:
    """Write scraped bbref bio rows to a CSV matching ``PlayerBio`` fields.

    Args:
        rows: Rows as returned by
            ``app.services.player_bio.bbref_scrape.scrape_letters`` (each a
            ``PlayerBio.__dict__``).
        out_path: Destination CSV path; parent directories are created.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_BIO_CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in _BIO_CSV_FIELDNAMES})


async def _run_bio_enrichment(
    *, year: int, league_id: str, player_ids: set[int] | None = None
) -> None:
    """Scrape and ingest Basketball-Reference bios for one venue's cohort.

    Mirrors ``scripts/bbref_bio_scraper.py --summer-league-year --summer-league
    -league-id`` followed by ``scripts/ingest_player_bios.py --file <csv>``:
    resolve the cohort's bbref-having slugs, scrape just those slugs to a
    freshly written CSV, then ingest exactly that CSV. Cohort players with no
    bbref id are flagged for manual review by the ingest step itself (it
    re-derives the same target set), not scraped.

    Args:
        year: Summer League competition year.
        league_id: NBA.com LeagueID to scope the cohort to.
        player_ids: Canonical players whose roster change should force enrichment;
            ``None`` retains the direct helper's all-cohort behavior.
    """
    async with SessionLocal() as db:
        targets = await select_bio_enrichment_targets(
            db,
            year=year,
            league_id=league_id,
            player_ids=player_ids,
            retry_unenriched=player_ids is not None,
        )

    if not targets.slugs:
        logger.info(
            "L%s bio: no bbref-having cohort slugs to scrape (%d flagged for manual review)",
            league_id,
            len(targets.manual_review_player_ids),
        )
        return

    rows = scrape_letters(
        letters=[],
        out_dir=BIO_OUT_DIR,
        throttle=BIO_SCRAPE_THROTTLE_SECONDS,
        timeout=BIO_SCRAPE_TIMEOUT_SECONDS,
        extra_slugs=sorted(targets.slugs),
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    csv_path = BIO_OUT_DIR / f"bbio_sl_{league_id}_{year}_{timestamp}.csv"
    _write_bio_csv(rows, csv_path)
    logger.info(
        "L%s bio: scraped %d bbref profile(s) -> %s", league_id, len(rows), csv_path
    )

    await ingest_player_bios_csv(
        csv_path=csv_path,
        cache_dir=BIO_CACHE_DIR,
        dry_run=False,
        verbose=False,
        overwrite_master=False,
        fix_ambiguities_path=None,
        create_missing=False,
        summer_league_year=year,
        summer_league_league_id=league_id,
    )
    logger.info("L%s bio: ingest complete for %s", league_id, csv_path.name)


async def _run_venue(
    db: AsyncSession,
    fetcher: RosterFetcher,
    *,
    year: int,
    league_id: str,
) -> tuple[bool, bool]:
    """Fetch and, if published, load + enrich one Summer League venue roster.

    Args:
        db: Async database session shared across venues this run.
        fetcher: Roster fetcher (shared HTTP settings across venues).
        year: Summer League season year.
        league_id: NBA.com LeagueID for this venue.

    Returns:
        A ``(published, failed)`` tuple. ``published`` is True when the venue
        had at least one rostered player this run. ``failed`` is True only
        for a genuine enrichment failure that happened *after* a published
        roster was found -- a fetch failure or a not-yet-published roster are
        both folded into ``published=False`` since there is nothing to load
        either way.
    """
    try:
        result: RosterRunResult = fetcher.fetch_run(
            year=year, league_id=league_id, out_dir=RAW_ROOT, force=True
        )
    except Exception as exc:
        logger.warning(
            "L%s: FETCH FAILED (%s: %s); skipping enrich this run",
            league_id,
            type(exc).__name__,
            exc,
        )
        return False, False

    if result.error and result.team_count == 0:
        logger.warning(
            "L%s: FETCH FAILED (%s); skipping enrich this run", league_id, result.error
        )
        return False, False

    if result.player_count == 0:
        logger.info("L%s: no roster published yet", league_id)
        return False, False

    # Skip the load entirely if ANY team page failed to fetch this run. The
    # loader cuts rosters for teams absent from the snapshot (empty-team cut);
    # a team whose page transiently failed contributes zero players and is
    # indistinguishable from a genuinely-removed team, so loading a partial
    # snapshot would wrongly CUT that team's whole roster. Skip and retry next
    # run, when all team pages fetch cleanly. Not a run failure — nothing was
    # written, and the next hourly run recovers.
    if result.error_count > 0:
        logger.warning(
            "L%s: %d of %d team page(s) failed to fetch; skipping load this run "
            "to avoid cutting rosters for transiently-absent teams (retry next run)",
            league_id,
            result.error_count,
            result.team_count,
        )
        return False, False

    logger.info(
        "L%s: %d players published across %d teams (%d team errors)",
        league_id,
        result.player_count,
        result.team_count,
        result.error_count,
    )

    try:
        entries = [
            RosterEntry(**player)
            for team in result.team_results
            for player in team.players
        ]
        venue = SUPPORTED_SUMMER_LEAGUES[league_id]
        competition = CompetitionKey(
            year=year, league_id=league_id, venue_slug=venue.slug
        )
        recorded_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # Each stage below commits independently (rather than one shared
        # transaction) so a later stage failing does not roll back an
        # already-durable earlier stage -- matching the original bash
        # pipeline, where each underlying script is its own process/commit.
        async with db.begin():
            diff_report = await load_roster_snapshot(
                db, competition, entries, recorded_at=recorded_at
            )
        logger.info(
            "L%s load: added=%d unchanged=%d cut=%d",
            league_id,
            diff_report.added,
            diff_report.unchanged,
            diff_report.cut,
        )

        resolution_report = await resolve_summer_league_players(
            db,
            year=year,
            league_id=league_id,
            create_stubs=True,
            before_candidate_search=db.commit,
        )
        await db.commit()
        logger.info(
            "L%s resolve: total=%d resolved=%d unresolved=%d stubs=%d",
            league_id,
            resolution_report.total_source_players,
            resolution_report.resolved_source_players,
            resolution_report.unresolved_source_players,
            resolution_report.stubs_created,
        )
        changed_source_ids = await changed_source_player_ids(
            db,
            year=year,
            league_id=league_id,
            recorded_at=recorded_at,
        )
        changed_player_ids = await canonical_player_ids(db, changed_source_ids)
        await db.commit()
        logger.info(
            "L%s enrichment scope: changed_players=%d",
            league_id,
            len(changed_player_ids),
        )

        async with db.begin():
            ext_id_report = await backfill_nba_stats_external_ids(db)
        logger.info(
            "L%s external ids: seeded=%d already_present=%d conflicts=%d",
            league_id,
            ext_id_report.seeded,
            ext_id_report.already_present,
            len(ext_id_report.conflicts),
        )

        async with db.begin():
            headshot_report = await backfill_nba_headshots(
                db, overwrite=False, validator=None
            )
        logger.info(
            "L%s headshots: set=%d skipped_existing=%d fallback=%d",
            league_id,
            headshot_report.set_count,
            headshot_report.skipped_existing,
            len(headshot_report.fallback),
        )

        await _run_bio_enrichment(
            year=year, league_id=league_id, player_ids=changed_player_ids
        )

        college_result = await run_college_stats_sweep(
            SessionLocal,
            only_missing=True,
            sl_cohort=True,
            sl_year=year,
            sl_league_id=league_id,
            sl_player_ids=changed_player_ids,
        )
        logger.info(
            "L%s college stats: attempted=%d scraped=%d skipped=%d failed=%d "
            "seasons=%d no_source=%d",
            league_id,
            college_result.players_attempted,
            college_result.players_scraped,
            college_result.players_skipped,
            college_result.players_failed,
            college_result.seasons_upserted,
            len(college_result.no_source),
        )
    except Exception as exc:
        logger.error("L%s enrichment failed: %s", league_id, exc, exc_info=True)
        return True, True

    return True, False


async def main() -> int:
    """Run the Summer League roster fetch + enrichment cycle for all venues.

    Returns:
        Exit code (0 for success, including the all-not-published no-op case;
        1 for a genuine failure).
    """
    start_time = datetime.now(timezone.utc)

    try:
        try:
            year = _resolve_year()
            league_ids = _resolve_league_ids()
        except ValueError as exc:
            logger.error("Invalid Summer League roster configuration: %s", exc)
            return 1

        logger.info(
            "Starting Summer League roster poll: year=%s league_ids=%s",
            year,
            ",".join(league_ids),
        )

        # Ensure every app.schemas module is imported before the loader maps
        # cross-table FKs (e.g. summer_league_team_entries.nba_team_id ->
        # nba_teams) that aren't otherwise pulled in by this process's own
        # import graph.
        load_schema_modules()

        failed = False
        fetcher = RosterFetcher(
            timeout=FETCH_TIMEOUT_SECONDS, delay_seconds=FETCH_DELAY_SECONDS
        )

        async with SessionLocal() as db:
            in_window = await is_summer_league_window_open(
                db, now=start_time, year=year
            )
            # The window check is read-only, but SQLAlchemy opens an ambient
            # transaction for those reads. Close it before either returning or
            # entering the venue pipeline, whose stages own their transactions.
            await db.commit()
            if not in_window:
                logger.info("Summer League roster poll: off-window (dormant) -- no-op")
                return 0

            for league_id in league_ids:
                _published, venue_failed = await _run_venue(
                    db, fetcher, year=year, league_id=league_id
                )
                failed = failed or venue_failed
    finally:
        await dispose_engine()

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    if failed:
        logger.error(
            "Summer League roster poll finished with failures in %.1fs", elapsed
        )
        return 1

    logger.info("Summer League roster poll finished successfully in %.1fs", elapsed)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

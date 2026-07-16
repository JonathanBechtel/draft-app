"""Targeted raw refresh for active and recently-final Summer League games.

Launch-readiness plan item 2, ``docs/plans/summer-league-desk-launch-readiness.md``
work-breakdown step 2 ("Targeted live box-score refresh"): the season raw
ingestor (:mod:`app.services.summer_league.raw_ingestion`) either reuses
whatever endpoint snapshot already exists on disk (possibly stale for a live
game or one that just finalized) or, with ``force=True``, redownloads every
game in the whole season/LeagueID -- overkill for a single live tick and
exactly the "redownloading history" this module exists to avoid.

This module adds the missing middle: find the handful of games that are
active or recently final within a time window around "now",
group them by (year, LeagueID) the way raw ingestion is scoped, and force a
fresh boxscore/pbp/shotchart pull for *only* those exact game IDs via
:attr:`~app.services.summer_league.raw_ingestion.RawIngestionOptions.game_ids`
(added alongside this module for exactly this purpose).

**Deliberately out of scope here** (spec step 3, a separate ticket):
normalizing the refreshed raw snapshots into
``summer_league_{team,player}_game_logs``/shot/PBP rows, or writing any Desk
projection. This module's job ends the moment raw JSON is on disk -- it never
touches a normalized or projection table.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
)
from app.services.summer_league.raw_ingestion import (
    NBAStatsJSONClient,
    RawIngestionOptions,
    SummerLeagueRawIngestor,
    SummerLeagueRequiredGamelogError,
    is_required_game_endpoint,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore

# Include a scored Final still inside the bounded tip-time window only when it
# has no normalized player lines. The scoreboard can flip to Final before the
# provider's last box snapshot is available; excluding that gap permanently
# stranded tonight's DAL-MEM game without a top performer. Healthy Finals and
# synthetic/unscored state anchors are not re-fetched.
# Fix #4: POSTPONED/CANCELED are deliberately absent -- a postponed game will
# never tip, so its critical box-score endpoints (boxscoretraditionalv2 etc.)
# would never return data. Selecting it here would trip fix #2's
# fail-the-whole-tick guard for every tick in its window. Since `map_game_status`
# now persists the real terminal status instead of collapsing it to SCHEDULED,
# this tuple excludes it with no extra filtering needed.
_LIVE_STATUSES = (
    SummerLeagueGameStatus.SCHEDULED,
    SummerLeagueGameStatus.IN_PROGRESS,
)

# How far before/after "now" an eligible game still counts as
# "active" and worth a forced refresh. #529 widened the schedule ingest to
# the full active-event horizon (games days/weeks out), so status alone is
# not enough -- a Scheduled game three weeks out is not "distant" by status,
# only by time. 6 hours covers a game that started late and is running long
# on one side, and a game about to tip (useful for the Morning Card's
# imminent state) on the other, without pulling the whole remaining slate.
DEFAULT_WINDOW_BEFORE = timedelta(hours=6)
DEFAULT_WINDOW_AFTER = timedelta(hours=6)


def _default_clock() -> datetime:
    """Return the current time as naive UTC, matching this schema's convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    """Normalize a possibly-aware datetime to naive UTC for column comparisons."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass(frozen=True)
class LiveGameSelection:
    """One active/recently-final game selected for a forced raw refresh."""

    nba_stats_game_id: str
    year: int
    league_id: str


@dataclass
class LiveIngestionReport:
    """Summary of one live-ingestion pass across every selected game group.

    Attributes:
        selected: Total games selected by :func:`select_active_window_games`.
        groups: Distinct (year, LeagueID) groups the selection was split into.
        written: Raw files actually (re)written across all groups.
        skipped: Raw files planned but not written (dry-run only; live
            ingestion never sets ``dry_run``, so this is normally 0).
        errors: Per-game/endpoint fetch errors, plus one entry per group that
            failed outright on a required season gamelog. Errors are never
            folded into ``written`` -- a failed endpoint cannot be reported
            as a successful refresh.
        required_errors: The subset of ``errors`` that must block freshness --
            a group's *required* season gamelog fetch failing outright
            (``SummerLeagueRequiredGamelogError``, the prerequisite every
            game in that group's normalize pass depends on), plus any
            critical per-game box-score endpoint failure for a selected game
            (``boxscoretraditionalv2``, ``boxscoreadvancedv2``,
            ``boxscorescoringv2`` --
            :func:`~app.services.summer_league.raw_ingestion.is_required_game_endpoint`).
            A failed critical box-score fetch leaves the OLD on-disk
            snapshot in place (``force=True`` only overwrites on a
            successful fetch), so normalize would otherwise silently re-read
            a stale player line as fresh. A non-blocking per-game/endpoint
            hiccup (``playbyplayv2``/``shotchartdetail``) does not count
            here; callers that need to decide whether a tick can safely
            proceed to normalize/claim fresh state should gate on this, not
            on ``errors``.
        error_messages: Human-readable detail for each counted error.
    """

    selected: int = 0
    groups: int = 0
    written: int = 0
    skipped: int = 0
    errors: int = 0
    required_errors: int = 0
    error_messages: list[str] = field(default_factory=list)


async def select_active_window_games(
    db: AsyncSession,
    *,
    now: datetime,
    window_before: timedelta = DEFAULT_WINDOW_BEFORE,
    window_after: timedelta = DEFAULT_WINDOW_AFTER,
) -> list[LiveGameSelection]:
    """Select active/recently-final games within a bounded window around ``now``.

    Recent Final games with no normalized player lines are included for a
    closing box-score pull. Healthy Finals are skipped. Games with no
    ``tip_datetime`` (only possible for legacy rows that predate scoreboard
    ingest, #529) are excluded too -- with no timestamp there is no way to
    tell "about to tip" from "three weeks out," so they are conservatively
    treated as not currently active rather than guessed at.

    Args:
        db: Async database session.
        now: The current time (naive UTC, or aware -- normalized either way).
        window_before: How far before ``now`` a game's ``tip_datetime`` may
            fall and still be selected.
        window_after: How far after ``now`` a game's ``tip_datetime`` may
            fall and still be selected.

    Returns:
        One :class:`LiveGameSelection` per matching game, ordered by
        ``tip_datetime`` (earliest first). Empty when nothing is active.
    """
    resolved_now = _naive_utc(now)
    window_start = resolved_now - window_before
    window_end = resolved_now + window_after
    has_player_lines = (
        select(SummerLeaguePlayerGameLog.id)  # type: ignore[call-overload]
        .where(SummerLeaguePlayerGameLog.game_id == SummerLeagueGame.id)
        .exists()
    )

    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeagueGame.nba_stats_game_id,
            SummerLeagueCompetition.year,
            SummerLeagueCompetition.league_id,
        )
        .join(
            SummerLeagueCompetition,
            SummerLeagueGame.competition_id == SummerLeagueCompetition.id,  # type: ignore[arg-type]
        )
        .where(
            or_(
                SummerLeagueGame.status.in_(_LIVE_STATUSES),  # type: ignore[attr-defined]
                and_(
                    SummerLeagueGame.status.in_(  # type: ignore[attr-defined]
                        (SummerLeagueGameStatus.FINAL,)
                    ),
                    or_(
                        SummerLeagueGame.home_score.is_not(None),  # type: ignore[union-attr]
                        SummerLeagueGame.away_score.is_not(None),  # type: ignore[union-attr]
                    ),
                    ~has_player_lines,
                ),
            ),
            SummerLeagueGame.tip_datetime.is_not(None),  # type: ignore[union-attr]
            SummerLeagueGame.tip_datetime >= window_start,  # type: ignore[operator]
            SummerLeagueGame.tip_datetime <= window_end,  # type: ignore[operator]
        )
        .order_by(SummerLeagueGame.tip_datetime)  # type: ignore[arg-type]
    )
    result = await db.execute(stmt)
    return [
        LiveGameSelection(
            nba_stats_game_id=row.nba_stats_game_id,
            year=row.year,
            league_id=row.league_id,
        )
        for row in result.all()
    ]


def group_by_year_league(
    selections: Sequence[LiveGameSelection],
) -> dict[tuple[int, str], list[str]]:
    """Group selected game IDs by (year, LeagueID), the raw ingestor's scope.

    Args:
        selections: Selected games, e.g. from :func:`select_active_window_games`.

    Returns:
        A mapping keyed by ``(year, league_id)`` to that group's game IDs, in
        the order they were encountered. Empty when ``selections`` is empty.
    """
    grouped: dict[tuple[int, str], list[str]] = {}
    for selection in selections:
        key = (selection.year, selection.league_id)
        grouped.setdefault(key, []).append(selection.nba_stats_game_id)
    return grouped


def refresh_selected_games(
    selections: Sequence[LiveGameSelection],
    *,
    client: NBAStatsJSONClient,
    store: SummerLeagueRawStore | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> LiveIngestionReport:
    """Force-refresh raw game endpoints for exactly the given selections.

    Groups ``selections`` by (year, LeagueID) and calls
    :meth:`~app.services.summer_league.raw_ingestion.SummerLeagueRawIngestor.fetch_year_league`
    once per group with ``force=True`` and ``game_ids`` set to that group's
    exact IDs -- never the whole season. An empty ``selections`` makes zero
    per-game (and zero group) network calls.

    A group whose required season gamelog fetch fails
    (:class:`~app.services.summer_league.raw_ingestion.SummerLeagueRequiredGamelogError`)
    is recorded as an error for that group and does not abort sibling
    groups -- one bad LeagueID/year combination cannot silently swallow every
    other live game's refresh. Likewise, a critical per-game box-score
    endpoint failure (see
    :func:`~app.services.summer_league.raw_ingestion.is_required_game_endpoint`)
    is folded into ``required_errors`` but does not stop this function from
    processing the rest of the group or sibling groups -- it is the caller
    (`scripts/sl_desk_tick.py`'s ``run_desk_tick``) that inspects
    ``required_errors`` afterward and aborts the whole tick before claiming
    fresh state.

    Args:
        selections: Exact games to refresh, e.g. from
            :func:`select_active_window_games`.
        client: NBA Stats client (protocol-typed; tests inject a fake).
        store: Raw snapshot store. Defaults to the standard on-disk store.
        sleep: Injectable sleep function for tests.
        progress: Optional progress callback, forwarded to the ingestor.

    Returns:
        Aggregate selected/written/skipped/error counts across every group.
    """
    report = LiveIngestionReport(selected=len(selections))
    grouped = group_by_year_league(selections)
    report.groups = len(grouped)
    if not grouped:
        return report

    resolved_store = store or SummerLeagueRawStore()
    ingestor = SummerLeagueRawIngestor(
        client=client, store=resolved_store, sleep=sleep, progress=progress
    )

    for (year, league_id), game_ids in sorted(grouped.items()):
        options = RawIngestionOptions(
            year=year,
            league_id=league_id,
            game_ids=tuple(game_ids),
            force=True,
        )
        try:
            manifest = ingestor.fetch_year_league(options)
        except SummerLeagueRequiredGamelogError as exc:
            report.errors += 1
            report.required_errors += 1
            report.error_messages.append(f"{year}/{league_id}: {exc}")
            continue
        report.written += len(manifest.files_written)
        report.skipped += len(manifest.files_skipped)
        report.errors += len(manifest.errors)
        # A critical box-score endpoint failure (traditional/advanced/
        # scoring) for a selected game leaves the stale on-disk snapshot in
        # place, so it must block freshness the same way a required-gamelog
        # failure does -- fold it into required_errors too, in addition to
        # the unconditional errors count above. playbyplayv2/shotchartdetail
        # failures stay in errors only.
        report.required_errors += sum(
            1 for error in manifest.errors if is_required_game_endpoint(error.endpoint)
        )
        report.error_messages.extend(
            f"{year}/{league_id}/{error.game_id or '-'}: {error.endpoint}: {error.message}"
            for error in manifest.errors
        )
    return report


async def run_live_ingestion(
    db: AsyncSession,
    *,
    client: NBAStatsJSONClient,
    store: SummerLeagueRawStore | None = None,
    clock: Callable[[], datetime] = _default_clock,
    window_before: timedelta = DEFAULT_WINDOW_BEFORE,
    window_after: timedelta = DEFAULT_WINDOW_AFTER,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
    before_refresh: Callable[[], Awaitable[None]] | None = None,
) -> LiveIngestionReport:
    """Select active-window games and force-refresh their raw endpoints.

    The end-to-end entry point: resolve "now" from ``clock``,
    select active/recently-final games in the active window
    (:func:`select_active_window_games`), and refresh exactly those
    (:func:`refresh_selected_games`). Never normalizes or writes any
    projection -- callers that need Desk state after this must run
    normalization/tick wiring separately.

    Args:
        db: Async database session (read-only here; no writes/commits).
        client: NBA Stats client (protocol-typed; tests inject a fake).
        store: Raw snapshot store. Defaults to the standard on-disk store.
        clock: Injectable "now" source for tests.
        window_before: How far before ``now`` a game may tip and still count.
        window_after: How far after ``now`` a game may tip and still count.
        sleep: Injectable sleep function for tests.
        progress: Optional progress callback, forwarded to the ingestor.
        before_refresh: Optional caller-owned transaction boundary invoked
            after selecting the games and before any NBA Stats request.
            Long-running cron callers use it to keep external I/O outside a
            database transaction; request code leaves it unset.

    Returns:
        Aggregate selected/written/skipped/error counts.
    """
    now = clock()
    selections = await select_active_window_games(
        db, now=now, window_before=window_before, window_after=window_after
    )
    # The caller's boundary releases a transaction-scoped writer lock.  Do
    # that only when the refresh below will issue provider requests; an empty
    # selection has no external I/O and must retain the caller's lock for its
    # remaining projection writes.
    if selections and before_refresh is not None:
        await before_refresh()
    return refresh_selected_games(
        selections, client=client, store=store, sleep=sleep, progress=progress
    )

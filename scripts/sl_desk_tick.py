"""Job B — the Summer League Desk hourly tick orchestrator.

`docs/plans/summer-league-scouts-desk-behavior-spec.md` §10 "Job B —
``sl_desk_tick``" pins the exact order this module wires together, every
step supplied by a sibling ticket's already-shipped, already-tested public
API — this module is pure orchestration, it implements no new grading,
storyline, or commentary logic of its own:

    0. schedule/scoreboard ingest (#515, #529) — upsert the active event's
       full known schedule (``tip_datetime`` / status / scores) before
       anything else, since the state machine and Morning Card can't exist
       without tip times, and every later step reads this as the
       provider-authoritative live status for each game.
    1. targeted live raw refresh (#531,
       ``app.services.summer_league.live_ingestion.run_live_ingestion``) --
       force a fresh boxscore/pbp/shotchart pull for exactly the
       Scheduled/In-Progress games sitting in an active time window around
       "now" (never the whole season). Runs *after* scoreboard so the
       selection reads each game's freshest known status, and *before*
       normalization so normalize always sees this tick's newest raw
       snapshot, never a stale one from an earlier hour.
    2. (existing) normalize -- ``normalize_competition_games`` /
       ``normalize_player_game_logs`` (the same functions
       ``scripts/normalize_summer_league.py`` calls) pick up any newly
       audited raw box scores for today's competitions. Best-effort per
       competition: a competition with no audited raw run yet this hour is
       not an error (raw fetch/audit runs on its own cadence, independent
       of this hourly tick). Status resolution here is pure and one-way
       (#530, ``normalization.resolve_game_status``) -- a partial mid-game
       snapshot can never promote a Scheduled/In-Progress game to Final on
       its own (only scoreboard, already reflected in the persisted status
       by tick order, or a fully ``COMPLETE`` audited historic backfill with
       no scoreboard tracking at all, ever does that), and a proven Final is
       monotonic against any later, less-complete call. Every competition
       normalize actually touched this tick is then passed to a *scoped*
       ``summer_league_player_seasons`` rebuild (#523,
       ``app.services.summer_league.metrics.rebuild`` called with
       ``competition_ids=``) so step 3's grading reads freshly recomputed
       event aggregates rather than whatever was materialized on a prior
       tick -- never the unscoped, whole-table wipe-and-rebuild
       ``scripts/rebuild_sl_metrics.py`` performs (far too heavy for an
       hourly cron, and destructive to any competition this tick didn't
       just normalize).
    3. per active roster player -> grade vs the active T1 baseline (#503,
       ``desk_grades.grade_players_bulk``, #548's bulk entry point -- ONE
       batched grading pass for the whole roster, not a per-player loop) -> T2.
    4. evaluate storyline triggers for today's games (#504,
       ``desk_storylines.compute_desk_storylines``) -> T3 + T4.
    5. commentary (#524) -- fire all eight #520 detectors for each graded
       player (``percentile``, ``cohort_rank``, ``streak``, ``self_delta``,
       ``leads_field``, ``debut_vs_bar``, ``count_club``, ``first_since``),
       each fed by a batched peer-population fetch from
       ``desk_fact_queries.py`` (never one query per player -- see that
       module's docstring for exactly how each is batched), and persist the
       resulting Facts onto T2 (#519/#548
       ``desk_commentary.persist_grade_facts_bulk``) and, grouped by
       tonight's rosters, onto each touched T4 slate row
       (``persist_slate_facts_bulk``) -- ONE batched select + ONE batched
       update each, never a per-player/per-game ``select``+``flush`` -- both
       of which run every fired Fact through Stage 2 selection
       (``desk_selection.dedup_facts`` / ``select_facts``, e.g. a rank-1
       ``cohort_rank`` subsuming its own ``percentile``) via
       ``desk_commentary.build_facts_payload``.
    6. render/state freshness -- upsert ``event_desk_state`` (#506
       ``event_desk.controller.run_event_desk_tick`` -- the only module that
       writes that table). Only reached once every *required* step above
       has genuinely succeeded (#530): if step 1's targeted refresh reports
       any error for a game it actually selected this tick, the whole tick
       raises before this step, so a failed refresh never gets to claim
       fresh state -- the caller's transaction rolls back and the next
       scheduled tick retries.
    7. render snapshot materialization (#551, launch-readiness item 10) --
       build the COMPLETE Preview/Live/Recap x Tracker cohort/stat-view
       variant matrix (``desk_read.build_desk_render_variants``) and
       atomically upsert every row in ONE bounded statement
       (``event_desk.render_snapshots.upsert_render_snapshots``), carrying
       step 6's just-stamped freshness. The FINAL step -- reached under the
       exact same "every required step above succeeded" guarantee as step 6,
       since it always runs immediately after it and shares the same
       caller-controlled transaction: a failure anywhere in steps 0-6 (or in
       this step itself) rolls back this tick's writes wholesale, so
       whatever the PRIOR successful tick materialized is never overwritten
       with a partial result. This is what lets the homepage read a single
       persisted snapshot at request time instead of reassembling the Desk
       (the pre-#551 ~71-query-per-request assembler,
       ``desk_read._assemble_desk_payload``) on every visit.

**Never rebuilds a distribution.** Job A (``scripts/build_sl_cohort_baselines.py``)
is the rare, offline cohort-baseline (T1) builder; this tick only ever reads
the currently active baseline version and fails loudly if none exists --
Job A must have run first.

**Off-window / dormant tick is inert.** Before touching the network or any
T2/T3/T4 table, the tick resolves the Summer League event's inner daily
state (Preview/Live/Recap) via the same pure state machine the framework
controller uses (`app.services.event_desk.state_machine.inner_state`).
When that resolves to ``None`` (the event is not in an Active or Wind-down
content window -- Dormant, Announced, Warm-up, or Archived), steps
0-7 are skipped entirely. The scheduler records a successful no-op with
``content_updated=false`` in ``summer_league_pipeline_states``, while
``event_desk_state`` and all render snapshots remain byte-for-byte unchanged.
If content has never been built, no state row is created merely to claim
freshness.

**#527 pre-anchor bootstrap.** The dormancy resolver above derives the
event's outer lifecycle phase from known ``summer_league_games`` rows, which
don't exist yet on the very first morning of the season (or any tick in the
announce/pre-roll window before step 0 has ever run) -- a chicken-and-egg
gap, since step 0 is exactly what would create that anchor. Before falling
back to the inert path, a resolved-``None`` tick calls
``_needs_scoreboard_bootstrap`` -- a synthetic-calendar check over each
target competition's configured ``starts_on``/``ends_on`` (**not** a network
call) -- and, only when that places the event in Announced/Warm-up/Active,
runs step 0 once and re-resolves. A genuinely off-window/dormant event
(``_needs_scoreboard_bootstrap`` returns ``False``) never reaches step 0
here, preserving #516's deliberate network-free guarantee for that case.

**Idempotent.** Every write this module performs delegates to an existing
upsert (``grade_players_bulk``, ``compute_desk_storylines``,
``persist_grade_facts_bulk``, ``persist_slate_facts_bulk``,
``run_event_desk_tick``, ``upsert_scoreboard_games``) -- re-running the tick
over the same data updates rows in place rather than duplicating them.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/sl_desk_tick.py
  scripts/with-db-env.sh conda run -n draftguru python scripts/sl_desk_tick.py \
      --raw-root data/raw/nba_stats/summer_league
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import nullcontext
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.schemas.event_desk import (  # noqa: E402
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
)
from app.schemas.player_affiliation import AffiliationStatus  # noqa: E402
from app.schemas.players_master import PlayerMaster  # noqa: E402
from app.schemas.summer_league import (  # noqa: E402
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueParticipation,
)
from app.schemas.summer_league_desk import (  # noqa: E402
    SummerLeagueCohortBaseline,
    SummerLeagueDeskGrain,
)
from app.services.event_desk.controller import run_event_desk_tick  # noqa: E402
from app.services.event_desk.lifecycle import lifecycle_phase  # noqa: E402
from app.services.event_desk.registry import (  # noqa: E402
    SUMMER_LEAGUE_REGISTRATION,
    DeskEvent,
    WindowPriors,
)
from app.services.event_desk.render_snapshots import (  # noqa: E402
    RenderSnapshotWrite,
    upsert_render_snapshots,
)
from app.services.event_desk.state_machine import inner_state  # noqa: E402
from app.services.event_desk.timeutils import to_eastern_date  # noqa: E402
from app.services.summer_league.cohort_baselines import cohort_key_for  # noqa: E402
from app.services.summer_league.desk_commentary import (  # noqa: E402
    persist_grade_facts_bulk,
    persist_slate_facts_bulk,
)
from app.services.summer_league.desk_fact_queries import (  # noqa: E402
    club_members_clearing,
    cohort_peers,
    count_club_threshold,
    field_peers,
    fetch_cohort_members,
    fetch_current_event_gp,
    fetch_debut_baselines,
    fetch_debut_status,
    fetch_event_baselines,
    fetch_game_baselines,
    fetch_game_lines,
    fetch_prior_events,
    fetch_tonight_field,
    most_recent_prior_holder,
)
from app.services.summer_league.desk_facts import (  # noqa: E402
    Fact,
    FactSubject,
    detect_cohort_rank,
    detect_count_club,
    detect_debut_vs_bar,
    detect_first_since,
    detect_leads_field,
    detect_percentile,
    detect_self_delta,
    detect_streak,
)
from app.services.summer_league.desk_grades import (  # noqa: E402
    GradeRow,
    grade_players_bulk,
)
from app.services.summer_league.desk_read import (  # noqa: E402
    _effective_now,
    build_desk_render_variants,
)
from app.services.summer_league.desk_storylines import (  # noqa: E402
    SlateRow,
    StorylineTickResult,
    compute_desk_storylines,
)
from app.services.summer_league.live_ingestion import (  # noqa: E402
    LiveIngestionReport,
    run_live_ingestion,
)
from app.services.summer_league.metrics import rebuild as rebuild_sl_metrics  # noqa: E402
from app.services.summer_league.nba_stats_client import NBAStatsClient  # noqa: E402
from app.services.summer_league.normalization import (  # noqa: E402
    normalize_competition_games,
    normalize_player_game_logs,
)
from app.services.summer_league.pipeline_state import (  # noqa: E402
    complete_pipeline,
    record_pipeline_failure,
    start_pipeline,
)
from app.services.summer_league.pipeline_telemetry import (  # noqa: E402
    PipelineTelemetry,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore  # noqa: E402
from app.services.summer_league.scoreboard_ingest import (  # noqa: E402
    ScoreboardIngestReport,
    resolve_target_competitions,
    run_scoreboard_ingest,
)
from app.services.summer_league.write_lock import (  # noqa: E402
    acquire_summer_league_writer_lock_bounded_timed,
)
from app.schemas.summer_league_pipeline import SummerLeaguePipelineJob  # noqa: E402
from app.utils.db_async import SessionLocal, engine  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_RAW_ROOT = Path("data/raw/nba_stats/summer_league")

# Maximum wall-clock time the Desk tick will wait for the shared writer lock
# before giving up (#622 -- a long-running full-ingestion cron holding the
# lock previously starved the Desk for over an hour). The Desk's total tick
# budget is two minutes with providers healthy; this bound leaves ample room
# for real work in both the initial acquire and each post-provider-I/O
# reacquisition while still failing fast enough for the next scheduled tick
# to retry promptly.
DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS = 30.0

# Roster statuses `desk_storylines.compute_desk_storylines` treats as "on the
# active roster tonight" (mirrored here so grading covers the same universe
# storylines will read T2 back for).
_ROSTER_ACTIVE_STATUSES = {
    AffiliationStatus.ANNOUNCED,
    AffiliationStatus.CONFIRMED,
    AffiliationStatus.ACTIVE,
}


@dataclass(frozen=True)
class DeskTickResult:
    """Summary of one :func:`run_desk_tick` call -- every stage's outcome."""

    now: datetime
    executed_at: datetime
    dormant: bool
    daily_state: Optional[EventDailyState]
    content_updated: bool
    source_refreshed: bool = False
    source_advanced: bool = False
    baseline_version: Optional[str] = None
    scoreboard_report: Optional[ScoreboardIngestReport] = None
    # Step 1 -- targeted live raw refresh (#531/#530). `None` on a dormant
    # tick (step 1 never runs there); otherwise always populated, including
    # the common empty-window case (`selected=0`).
    live_refresh_report: Optional[LiveIngestionReport] = None
    normalized_competition_ids: tuple[int, ...] = ()
    graded_player_ids: tuple[int, ...] = ()
    storyline_results: dict[int, StorylineTickResult] = field(default_factory=dict)
    event_desk_states: tuple[EventDeskState, ...] = ()
    # Whether the #527 pre-anchor bootstrap (`_needs_scoreboard_bootstrap`) ran
    # `run_scoreboard_ingest` before the normal daily-state resolution succeeded.
    bootstrapped: bool = False
    # Step 7 -- render snapshot materialization (#551, launch-readiness item
    # 10). The number of `(daily_state, tracker_cohort, tracker_stat_view)`
    # variant rows upserted this tick -- 0 off-window (nothing to
    # materialize), otherwise `len(DESK_RENDER_DAILY_STATES) *
    # len(TRACKER_COHORTS) * len(TRACKER_STAT_VIEWS)` (72 today).
    materialized_variant_count: int = 0


async def _resolve_daily_state(
    db: AsyncSession, *, now: datetime
) -> Optional[EventDailyState]:
    """Cheap pre-check: is the SL event's inner daily state resolvable right now?

    Mirrors the per-registration resolution
    ``app.services.event_desk.controller.run_event_desk_tick`` performs
    internally (that helper is private to the controller module) so this
    tick can decide, *before* touching the network or any T2/T3/T4 table,
    whether it's off-window and therefore inert. Wind-down is content-active
    even though the inner state machine only accepts Active events, so this
    pre-check maps Wind-down directly to Recap, matching the request path.
    ``registration.sync`` is the same
    idempotent ``events`` row upsert the controller's own first step
    performs -- the only "write" this pre-check does.

    Args:
        db: Active database session.
        now: The tick's reference instant (naive UTC).

    Returns:
        The resolved daily state, or ``None`` outside the Active/Wind-down
        content window.
    """
    registration = SUMMER_LEAGUE_REGISTRATION
    event_row = await registration.sync(db, now.date())
    calendar_facts = await registration.provider.resolve_calendar_facts(db, now=now)
    desk_event = DeskEvent(
        key=registration.key,
        priority=event_row.priority,
        window_priors=WindowPriors.from_dict(event_row.window_priors),
        game_dates=calendar_facts.game_dates,
    )
    if lifecycle_phase(now, desk_event) == EventLifecyclePhase.WINDDOWN:
        return EventDailyState.RECAP
    return inner_state(
        now, calendar_facts.today_schedule, calendar_facts.today_statuses, desk_event
    )


# Outer lifecycle phases #527's bootstrap should attempt for: the event is on
# the calendar (Announced), imminent (Warm-up), or literally its first known
# day (Active) -- as opposed to Dormant (nowhere near the window) or Archived
# (long over), which must stay network-free per #516's cost decision.
_BOOTSTRAP_ELIGIBLE_PHASES = frozenset(
    {
        EventLifecyclePhase.ANNOUNCED,
        EventLifecyclePhase.WARMUP,
        EventLifecyclePhase.ACTIVE,
    }
)


def _synthetic_calendar_dates(
    competitions: Sequence[SummerLeagueCompetition],
) -> tuple[date, ...]:
    """Every day spanned by each competition's configured ``starts_on``/``ends_on``.

    Used only as a stand-in for real ``summer_league_games`` dates when a
    competition genuinely has zero game rows yet -- `lifecycle_phase`'s
    gap-bridge clustering (`app.services.event_desk.lifecycle`) sees an empty
    calendar as "far off," which is the #527 chicken-and-egg bug: no games
    yet means no anchor to even try the schedule feed that would create them.
    A competition missing either date contributes nothing (there's no
    fallback anchor to synthesize for it).

    Args:
        competitions: The target competitions for today (`resolve_target_competitions`).

    Returns:
        Every date in each competition's inclusive ``[starts_on, ends_on]``
        span, possibly empty (and possibly containing duplicates across
        competitions -- `lifecycle_phase`'s clustering dedupes internally).
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


async def _needs_scoreboard_bootstrap(db: AsyncSession, *, now: datetime) -> bool:
    """#527 -- should this off-window tick still attempt scoreboard ingest?

    ``_resolve_daily_state`` resolves ``None`` (inert) both for a genuinely
    dormant event *and* for an event whose window has arrived but has zero
    ``summer_league_games`` rows yet to anchor `lifecycle_phase`'s gap-bridge
    clustering (the very first morning of the season, before Job B step 0 has
    ever run) -- a chicken-and-egg gap, since step 0 is exactly what would
    create that anchor. This helper distinguishes the two: a real game
    already exists (the normal resolver is authoritative -- no bootstrap
    needed, `run_desk_tick`'s later steps already handle it), or a
    configured ``starts_on``/``ends_on`` places the event's *outer* lifecycle
    phase in Announced/Warm-up/Active (:data:`_BOOTSTRAP_ELIGIBLE_PHASES`) --
    only then does this return ``True``. A competition with no configured
    dates either, or one whose synthetic phase is Dormant/Wind-down/Archived,
    returns ``False`` and the tick stays network-free (preserves #516's
    deliberate off-window cost decision).

    Args:
        db: Active database session (caller controls the transaction).
        now: The tick's reference instant (naive UTC).

    Returns:
        Whether `run_desk_tick` should run `run_scoreboard_ingest` before
        re-attempting `_resolve_daily_state`.
    """
    today = to_eastern_date(now)
    competitions = await resolve_target_competitions(db, today=today)
    if not competitions:
        return False

    competition_ids = [c.id for c in competitions if c.id is not None]
    if competition_ids:
        has_games_stmt = (
            select(SummerLeagueGame.id)  # type: ignore[call-overload]
            .where(SummerLeagueGame.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
            .limit(1)
        )
        if (await db.execute(has_games_stmt)).first() is not None:
            # A real anchor already exists somewhere in this year's
            # competitions; the normal resolver is authoritative.
            return False

    synthetic_dates = _synthetic_calendar_dates(competitions)
    if not synthetic_dates:
        return False

    registration = SUMMER_LEAGUE_REGISTRATION
    event_row = await registration.sync(db, today)
    synthetic_event = DeskEvent(
        key=registration.key,
        priority=event_row.priority,
        window_priors=WindowPriors.from_dict(event_row.window_priors),
        game_dates=synthetic_dates,
    )
    return lifecycle_phase(now, synthetic_event) in _BOOTSTRAP_ELIGIBLE_PHASES


async def _active_baseline_version(db: AsyncSession) -> Optional[str]:
    """The currently active T1 ``baseline_version``, or ``None`` if Job A hasn't run."""
    stmt = (
        select(SummerLeagueCohortBaseline.baseline_version)  # type: ignore[call-overload]
        .where(SummerLeagueCohortBaseline.is_active.is_(True))  # type: ignore[attr-defined]
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


async def _normalize_competition(
    db: AsyncSession, competition: SummerLeagueCompetition, *, raw_root: Path
) -> bool:
    """Best-effort normalize one competition's audited raw data (Job B step 1).

    A competition with no audited raw run yet this hour is the common case
    (raw fetch/audit runs on its own cadence, independent of this hourly
    tick) -- not an error. ``normalize_competition_games`` raises
    ``ValueError`` when no ``summer_league_raw_runs`` row exists yet for
    ``(year, league_id)``, and file-backed parsing raises
    ``FileNotFoundError`` when the raw snapshot files aren't on disk. Both
    are caught here and treated as "nothing new to normalize this tick"
    rather than aborting the whole run.

    Args:
        db: Active database session.
        competition: The competition to normalize.
        raw_root: Root directory of audited raw Summer League snapshots.

    Returns:
        Whether normalization actually ran.
    """
    try:
        await normalize_competition_games(
            db,
            year=competition.year,
            league_id=competition.league_id,
            raw_root=raw_root,
        )
        await normalize_player_game_logs(
            db,
            year=competition.year,
            league_id=competition.league_id,
            raw_root=raw_root,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.info(
            "sl_desk_tick: skip normalize for %s/%s (%s)",
            competition.year,
            competition.league_id,
            exc,
        )
        return False
    return True


async def _active_roster_player_ids(db: AsyncSession, competition_id: int) -> list[int]:
    """Every distinct player_id with an active roster row for ``competition_id``."""
    stmt = select(  # type: ignore[call-overload]
        SummerLeagueParticipation.player_id, SummerLeagueParticipation.roster_status
    ).where(
        SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
        SummerLeagueParticipation.player_id.is_not(None),  # type: ignore[union-attr]
    )
    rows = (await db.execute(stmt)).all()
    ordered: dict[int, None] = {}
    for player_id, roster_status in rows:
        if player_id is None or roster_status not in _ROSTER_ACTIVE_STATUSES:
            continue
        ordered[player_id] = None
    return list(ordered.keys())


async def _game_roster_player_ids(
    db: AsyncSession, *, competition_id: int, game_date: date
) -> dict[int, list[int]]:
    """Map each of ``game_date``'s games in ``competition_id`` to its rostered player_ids.

    Reads the same roster shape ``desk_storylines.compute_desk_storylines``
    builds internally (not exposed by that module) so commentary Facts can
    be grouped per game for :func:`~app.services.summer_league.desk_commentary.persist_slate_facts`.
    """
    games = (
        (
            await db.execute(
                select(SummerLeagueGame).where(
                    SummerLeagueGame.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueGame.game_date == game_date,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    if not games:
        return {}

    team_entry_ids = {
        tid
        for g in games
        for tid in (g.home_team_entry_id, g.away_team_entry_id)
        if tid is not None
    }
    roster_rows = (
        (
            await db.execute(
                select(  # type: ignore[call-overload]
                    SummerLeagueParticipation.team_entry_id,
                    SummerLeagueParticipation.player_id,
                    SummerLeagueParticipation.roster_status,
                ).where(
                    SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueParticipation.team_entry_id.in_(team_entry_ids),  # type: ignore[attr-defined]
                    SummerLeagueParticipation.player_id.is_not(None),  # type: ignore[union-attr]
                )
            )
        ).all()
        if team_entry_ids
        else []
    )
    roster_by_team: dict[int, list[int]] = {}
    for team_entry_id, player_id, roster_status in roster_rows:
        if roster_status not in _ROSTER_ACTIVE_STATUSES or player_id is None:
            continue
        roster_by_team.setdefault(team_entry_id, []).append(player_id)

    out: dict[int, list[int]] = {}
    for g in games:
        assert g.id is not None
        ids = list(
            dict.fromkeys(
                roster_by_team.get(g.home_team_entry_id or -1, [])
                + roster_by_team.get(g.away_team_entry_id or -1, [])
            )
        )
        out[g.id] = ids
    return out


async def _commentary_for_competition(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    baseline_version: str,
    game_date: date,
    grade_by_player: dict[int, GradeRow],
    slate: Sequence[SlateRow],
) -> None:
    """Job B step 4 -- all eight #520 Facts onto graded T2 rows and their T4 game rows.

    Wires every detector `desk_facts.py` (#520) ships, each fed by the
    batched read layer `desk_fact_queries.py` (#524) -- ``percentile``
    (always fires, straight off the T2 :class:`GradeRow`), ``cohort_rank``,
    ``streak``, ``self_delta``, ``leads_field``, ``debut_vs_bar``,
    ``count_club``, and ``first_since``. Every extra read here is issued
    ONCE for the whole competition/roster this tick, never once per player
    (module docstring's CRITICAL constraint) -- see `desk_fact_queries.py`
    for the exact batching per query.

    Args:
        db: Active database session.
        competition: The competition this tick is grading/storylining --
            ``.year`` scopes prior-event/debut-status/count-club lookups.
        baseline_version: The T1 baseline version graded/storylines ran against.
        game_date: Today's (Eastern) slate date.
        grade_by_player: Every player graded this tick for this competition
            (Job B step 2's output).
        slate: This competition's ranked slate rows (Job B step 3's output,
            ``desk_storylines.SlateRow``) -- persisted onto only if non-empty.
    """
    if not grade_by_player:
        return

    assert competition.id is not None
    competition_id = competition.id
    player_ids = list(grade_by_player.keys())

    players = (
        (
            await db.execute(
                select(PlayerMaster).where(  # type: ignore[call-overload]
                    PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    player_by_id = {p.id: p for p in players if p.id is not None}
    label_by_id = {p.id: (p.display_name or f"Player {p.id}") for p in players}

    # -- Batched context fetches (once per competition/tick, never per player) --
    cohort_keys = {g.cohort_key for g in grade_by_player.values()}
    event_baselines = await fetch_event_baselines(
        db, baseline_version=baseline_version, cohort_keys=list(cohort_keys)
    )
    cohort_members_by_key = await fetch_cohort_members(
        db, baselines_by_cohort=event_baselines
    )

    debut_status = await fetch_debut_status(
        db, player_ids=player_ids, before_year=competition.year
    )
    debut_cohort_keys = {
        cohort_key_for(
            player_by_id[pid].draft_round,
            player_by_id[pid].draft_pick,
            grain=SummerLeagueDeskGrain.DEBUT,
        )
        for pid, is_debut in debut_status.items()
        if is_debut and pid in player_by_id
    }
    debut_baselines = await fetch_debut_baselines(
        db, baseline_version=baseline_version, cohort_keys=list(debut_cohort_keys)
    )

    prior_events = await fetch_prior_events(
        db, player_ids=player_ids, before_year=competition.year
    )
    current_gp_by_player = await fetch_current_event_gp(
        db, player_ids=player_ids, year=competition.year
    )

    # Streak's per-game bar/percentile ranks against the game-grain baseline
    # (#525), not the event-grain one `baseline_by_player` used to point at --
    # a separate cohort-key map since a player's game-grain key (`game:...`)
    # differs from their event-grain key (`slot:.../round:.../status:...`)
    # even though both derive from the same draft slot.
    game_cohort_key_by_player = {
        pid: cohort_key_for(
            player_by_id[pid].draft_round,
            player_by_id[pid].draft_pick,
            grain=SummerLeagueDeskGrain.GAME,
        )
        for pid in player_ids
        if pid in player_by_id
    }
    game_baselines = await fetch_game_baselines(
        db,
        baseline_version=baseline_version,
        cohort_keys=list(set(game_cohort_key_by_player.values())),
    )
    baseline_by_player = {
        pid: game_baselines.get(game_cohort_key_by_player[pid])
        for pid in player_ids
        if pid in game_cohort_key_by_player
    }
    game_lines_by_player = await fetch_game_lines(
        db,
        player_ids=player_ids,
        competition_id=competition_id,
        game_date=game_date,
        baseline_by_player=baseline_by_player,
    )

    field_entries = await fetch_tonight_field(
        db, competition_id=competition_id, game_date=game_date
    )
    field_value_by_player = {e.player_id: e.value for e in field_entries}

    fact_by_player: dict[int, list[Fact]] = {}
    for player_id, grade in grade_by_player.items():
        subject = FactSubject(
            player_id=player_id,
            player_label=label_by_id.get(player_id, f"Player {player_id}"),
            competition_id=competition_id,
        )
        facts: list[Fact] = [detect_percentile(subject=subject, grade=grade)]

        members = cohort_members_by_key.get(grade.cohort_key, [])
        baseline = event_baselines.get(grade.cohort_key)

        cohort_rank_fact = detect_cohort_rank(
            subject=subject,
            subject_value=grade.subject_value,
            metric="gmsc",
            cohort_key=grade.cohort_key,
            peers=cohort_peers(members, exclude_player_id=player_id),
            baseline_version=baseline_version,
        )
        if cohort_rank_fact is not None:
            facts.append(cohort_rank_fact)

        if baseline is not None:
            threshold = count_club_threshold(baseline)
            if threshold is not None:
                count_club_fact = detect_count_club(
                    subject=subject,
                    metric="gmsc",
                    cohort_key=grade.cohort_key,
                    subject_value=grade.subject_value,
                    threshold=threshold,
                    since_year=_season_range_start(baseline.season_range),
                    other_members=club_members_clearing(
                        members, exclude_player_id=player_id, threshold=threshold
                    ),
                    baseline_version=baseline_version,
                )
                if count_club_fact is not None:
                    facts.append(count_club_fact)

                # `detect_first_since` always returns a Fact -- it's the
                # caller's job to only invoke it when the subject itself
                # clears the same qualifying bar (else it would read as a
                # superlative for an unremarkable performance).
                if grade.subject_value >= threshold:
                    prior_holder = most_recent_prior_holder(
                        members,
                        exclude_player_id=player_id,
                        threshold=threshold,
                        before_year=competition.year,
                    )
                    facts.append(
                        detect_first_since(
                            subject=subject,
                            metric="gmsc",
                            cohort_key=grade.cohort_key,
                            subject_value=grade.subject_value,
                            current_year=competition.year,
                            since_year=_season_range_start(baseline.season_range),
                            most_recent_prior=prior_holder,
                            baseline_version=baseline_version,
                        )
                    )

        streak_fact = detect_streak(
            subject=subject,
            metric="gmsc",
            cohort_key=game_cohort_key_by_player.get(player_id, grade.cohort_key),
            games=game_lines_by_player.get(player_id, []),
            baseline_version=baseline_version,
        )
        if streak_fact is not None:
            facts.append(streak_fact)

        prior = prior_events.get(player_id)
        if prior is not None:
            self_delta_fact = detect_self_delta(
                subject=subject,
                metric="gmsc",
                cohort_key=grade.cohort_key,
                current_value=grade.subject_value,
                current_gp=current_gp_by_player.get(player_id, 0),
                prior=prior,
                baseline_version=baseline_version,
            )
            if self_delta_fact is not None:
                facts.append(self_delta_fact)

        if debut_status.get(player_id) and player_id in player_by_id:
            player = player_by_id[player_id]
            debut_key = cohort_key_for(
                player.draft_round, player.draft_pick, grain=SummerLeagueDeskGrain.DEBUT
            )
            debut_baseline = debut_baselines.get(debut_key)
            if debut_baseline is not None:
                facts.append(
                    detect_debut_vs_bar(
                        subject=subject,
                        metric="gmsc",
                        debut_cohort_key=debut_key,
                        subject_value=grade.subject_value,
                        debut_bar=debut_baseline.mean_value,
                        baseline_version=baseline_version,
                    )
                )

        subject_field_value = field_value_by_player.get(player_id)
        if subject_field_value is not None:
            leads_field_fact = detect_leads_field(
                subject=subject,
                subject_value=subject_field_value,
                metric="gmsc",
                field_label="tonight's slate",
                field=field_peers(field_entries, exclude_player_id=player_id),
            )
            if leads_field_fact is not None:
                facts.append(leads_field_fact)

        fact_by_player[player_id] = facts

    # One batched select + one batched update for every graded player's T2
    # row (#548) -- never a per-player `select`+`flush` inside this loop.
    await persist_grade_facts_bulk(
        db,
        competition_id=competition_id,
        baseline_version=baseline_version,
        facts_by_player=fact_by_player,
    )

    if not slate:
        return

    roster_by_game = await _game_roster_player_ids(
        db, competition_id=competition_id, game_date=game_date
    )
    facts_by_game: dict[int, list[Fact]] = {
        slate_row.game_id: [
            fact
            for pid in roster_by_game.get(slate_row.game_id, [])
            for fact in fact_by_player.get(pid, [])
        ]
        for slate_row in slate
    }
    hero_game_ids = [slate_row.game_id for slate_row in slate if slate_row.is_hero]
    # One batched select + one batched update for every touched T4 slate row
    # (#548) -- never a per-game `select`+`flush` inside this loop.
    await persist_slate_facts_bulk(
        db, facts_by_game=facts_by_game, hero_game_ids=hero_game_ids
    )


def _season_range_start(season_range: str) -> int:
    """``"2017-2025"`` -> ``2017`` (mirrors ``cohort_baselines._parse_season_range``)."""
    start_str, _sep, _end_str = season_range.partition("-")
    return int(start_str)


async def _materialize_render_snapshots(db: AsyncSession, *, now: datetime) -> int:
    """Job B step 7 (#551, launch-readiness item 10) -- the tick's FINAL step.

    Builds the complete Preview/Live/Recap x Tracker cohort/stat-view variant
    matrix (`desk_read.build_desk_render_variants`) and atomically upserts
    every row in ONE bounded statement
    (`render_snapshots.upsert_render_snapshots`). Called only after step 6
    (`run_event_desk_tick`) has already stamped this tick's freshness, so the
    variants persisted here carry that same freshness stamp -- and only ever
    reached at all when every earlier *required* step in `run_desk_tick`
    genuinely succeeded, since a required failure raises before execution
    ever gets here and the caller's transaction (`db.begin()` in `main`
    below) rolls back this tick's writes wholesale, leaving whatever the
    prior successful tick wrote untouched.

    `source_freshness_tick_at`/`source_freshness_next_tick_eta` are read off
    each variant's own freshly-assembled `DeskFreshness` (`view.payload.
    freshness`) rather than re-queried -- every variant in one tick shares
    the identical freshness stamp by construction (`build_desk_render_variants`
    resolves it once and reuses it across the whole matrix).

    Args:
        db: Active database session (caller controls the transaction; never
            commits here -- delegates to `upsert_render_snapshots`, which
            also never commits).
        now: The tick's reference instant (naive UTC) -- forwarded to
            `build_desk_render_variants` unchanged.

    Returns:
        The number of variant rows upserted -- 0 when the event is
        off-window (nothing to materialize; any prior snapshots are left
        untouched, never truncated).
    """
    result = await build_desk_render_variants(db, now=now, now_is_effective=True)
    if result is None:
        return 0
    event_id, variants = result

    writes = [
        RenderSnapshotWrite(
            event_id=event_id,
            daily_state=variant.daily_state,
            tracker_cohort=variant.tracker_cohort,
            tracker_stat_view=variant.tracker_stat_view,
            view=variant.view,
            source_freshness_tick_at=(
                variant.view.payload.freshness.last_tick_at
                if variant.view.payload is not None
                else None
            ),
            source_freshness_next_tick_eta=(
                variant.view.payload.freshness.next_tick_eta
                if variant.view.payload is not None
                else None
            ),
        )
        for variant in variants
    ]
    await upsert_render_snapshots(db, writes, now=now)
    return len(writes)


async def run_desk_tick(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    raw_root: Path = DEFAULT_RAW_ROOT,
    client: Optional[NBAStatsClient] = None,
    release_transactions_for_network_io: bool = False,
    telemetry: PipelineTelemetry | None = None,
    writer_lock_max_wait_seconds: float = DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS,
) -> DeskTickResult:
    """Job B -- the Summer League Desk hourly tick (module docstring has the full order).

    Does not commit by default; the caller controls the transaction. The
    production cron passes ``release_transactions_for_network_io=True`` so
    provider fetches never run while an idle database transaction is open.

    Args:
        db: Active database session (caller controls the transaction).
        now: Override for "now" (tests only); defaults to the current UTC
            instant.
        raw_root: Root directory of audited raw Summer League snapshots,
            forwarded to the normalize and targeted-live-refresh steps.
        client: Optional injected :class:`NBAStatsClient` (tests only),
            forwarded to the scoreboard ingest and targeted live-refresh
            steps (they share one client/session); when omitted a real
            client is opened for the duration of those steps and closed
            afterward.
        release_transactions_for_network_io: Commit completed read/write work
            before provider requests, then reacquire the transaction-scoped
            writer lock before normalized/projection writes. This is required
            for long-running cron execution and is opt-in to preserve the
            legacy test/service caller transaction contract.
        telemetry: Optional production-run timer that emits one structured
            duration record for every major pipeline stage.
        writer_lock_max_wait_seconds: Maximum wall-clock time to wait for the
            shared writer lock -- both the initial acquire and each
            post-provider-I/O reacquisition -- before giving up (#622).
            Defaults to :data:`DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS`.

    Returns:
        A :class:`DeskTickResult` summarizing every stage's outcome.

    Raises:
        RuntimeError: The tick is not off-window (there's real work to do)
            but no active T1 cohort baseline exists -- Job A
            (``scripts/build_sl_cohort_baselines.py``) must run first; or
            the targeted live raw refresh (step 1) reported an error for a
            game it actually selected this tick -- a required refresh
            failure must never let the tick reach step 6 and claim fresh
            state (#530).
        SummerLeagueWriterLockTimeout: The writer lock (initial acquire or a
            post-provider-I/O reacquisition) was not obtained within
            ``writer_lock_max_wait_seconds`` -- a long-running lower-priority
            writer (typically full ingestion) is holding it. This is a
            retry-next-scheduled-run condition, not a data-quality failure
            (#622): the caller's transaction rolls back and no partial state
            is ever claimed as fresh.
    """
    # Serialize before either scheduled writer touches shared Summer League
    # identities/projections. The lower-priority full ingestor uses a
    # non-blocking attempt and skips its DB phase when this tick is active,
    # preventing the cross-cron source-player deadlock observed in production.
    # Bounded (#622): this tick must never block past an explicit maximum no
    # matter how long a lower-priority writer holds the lock.
    with (
        telemetry.step("writer_lock_wait") if telemetry is not None else nullcontext()
    ) as step_fields:
        await acquire_summer_league_writer_lock_bounded_timed(
            db,
            max_wait_seconds=writer_lock_max_wait_seconds,
            step_fields=step_fields,
        )

    async def reacquire_writer_lock() -> None:
        """Start a short serialized write phase after external provider I/O."""
        with (
            telemetry.step("writer_lock_reacquire")
            if telemetry is not None
            else nullcontext()
        ) as step_fields:
            await acquire_summer_league_writer_lock_bounded_timed(
                db,
                max_wait_seconds=writer_lock_max_wait_seconds,
                step_fields=step_fields,
            )

    # A contended lock can delay the tick across a tip/final boundary. Read the
    # production clock only after the wait so phase and freshness calculations
    # describe the instant this transaction can actually begin its work. Tests
    # keep their explicit deterministic override unchanged.
    executed_at = now if now is not None else datetime.utcnow()
    resolved_now = _effective_now(executed_at, scheduled_write=True)

    daily_state = await _resolve_daily_state(db, now=resolved_now)

    # #527 -- the very first morning of the season (or any tick in the
    # announce/pre-roll window) has zero `summer_league_games` rows to anchor
    # the resolver above, so it comes back dormant even though step 0 is
    # exactly what would create that anchor. Attempt the bootstrap ingest
    # once and re-resolve before falling back to the dormant/inert path --
    # a genuinely off-window tick (`_needs_scoreboard_bootstrap` returns
    # `False`) never reaches `run_scoreboard_ingest` here, preserving #516's
    # network-free guarantee for the truly dormant case.
    bootstrap_report: Optional[ScoreboardIngestReport] = None
    if daily_state is None and await _needs_scoreboard_bootstrap(db, now=resolved_now):
        with (
            telemetry.step("bootstrap_scoreboard_ingest")
            if telemetry is not None
            else nullcontext()
        ):
            bootstrap_report = await run_scoreboard_ingest(
                db,
                today=to_eastern_date(resolved_now),
                client=client,
                before_fetch=db.commit if release_transactions_for_network_io else None,
                before_upsert=(
                    reacquire_writer_lock
                    if release_transactions_for_network_io
                    else None
                ),
            )
        daily_state = await _resolve_daily_state(db, now=resolved_now)

    if daily_state is None:
        # Off-window/dormant: content-state inert. The registration pre-check
        # may sync the canonical event row, but lifecycle/content state and
        # render snapshots are left exactly as they were. Scheduler success is
        # recorded separately by the CLI's pipeline-state projection.
        with (
            telemetry.step(
                "dormant_noop",
                executed_at=executed_at.isoformat(),
                effective_data_at=resolved_now.isoformat(),
                content_updated=False,
            )
            if telemetry is not None
            else nullcontext()
        ):
            pass
        return DeskTickResult(
            now=resolved_now,
            executed_at=executed_at,
            dormant=True,
            daily_state=None,
            content_updated=False,
            event_desk_states=(),
            bootstrapped=bootstrap_report is not None,
            materialized_variant_count=0,
        )

    baseline_version = await _active_baseline_version(db)
    if baseline_version is None:
        raise RuntimeError(
            "No active Summer League cohort baseline (T1) found -- run "
            "scripts/build_sl_cohort_baselines.py before the desk tick."
        )

    today = to_eastern_date(resolved_now)

    # The transaction-scoped advisory lock above is intentionally released
    # before provider I/O. Keeping it (and a DB transaction) open while NBA
    # Stats retries can take minutes trips the production idle-in-transaction
    # guard and leaves no healthy connection for the later write phase.
    if release_transactions_for_network_io:
        await db.commit()

    # Steps 0-1 share one NBA Stats client/session -- opened here (once) when
    # the caller didn't inject one, and always closed afterward, mirroring
    # the owns-client pattern each individual step manages internally when
    # called standalone.
    owns_client = client is None
    active_client = client or NBAStatsClient()
    try:
        # Step 0 -- schedule/scoreboard ingest. Already ran above if this
        # tick needed the #527 bootstrap; skip the duplicate network
        # round-trip.
        if bootstrap_report is not None:
            scoreboard_report = bootstrap_report
        else:
            with (
                telemetry.step("scoreboard_ingest")
                if telemetry is not None
                else nullcontext()
            ):
                scoreboard_report = await run_scoreboard_ingest(
                    db,
                    today=today,
                    client=active_client,
                    before_fetch=(
                        db.commit if release_transactions_for_network_io else None
                    ),
                    before_upsert=(
                        reacquire_writer_lock
                        if release_transactions_for_network_io
                        else None
                    ),
                )

        # Step 1 -- targeted live raw refresh (#531/#530). Reads
        # `summer_league_games` fresh (including anything step 0 -- or the
        # #527 bootstrap above -- just flushed this tick), so a Scheduled
        # game bootstrapped moments ago is already visible here; selection
        # is status/window-scoped, not tied to `competitions` below.
        with (
            telemetry.step("live_raw_refresh")
            if telemetry is not None
            else nullcontext()
        ):
            live_refresh_report = await run_live_ingestion(
                db,
                client=active_client,
                store=SummerLeagueRawStore(raw_root),
                clock=lambda: resolved_now,
                before_refresh=(
                    db.commit if release_transactions_for_network_io else None
                ),
            )
        if live_refresh_report.required_errors > 0:
            # A group's *required* season gamelog fetch failed outright for
            # games this tick actually selected -- every subsequent
            # normalize pass for that group would run with no season-level
            # anchor at all. Raise before any T1-T4 write or the step 6
            # freshness stamp so the caller's transaction rolls back cleanly
            # and the next scheduled tick retries from the prior good state
            # (#530). A merely optional per-game/endpoint hiccup (reflected
            # in `.errors` but not `.required_errors`) does not abort the
            # tick -- normalize already tolerates partial per-game raw data.
            raise RuntimeError(
                "Required Summer League live raw refresh failed "
                f"({live_refresh_report.required_errors} required error(s)): "
                f"{'; '.join(live_refresh_report.error_messages)}"
            )
    finally:
        if owns_client:
            active_client.close()

    # The commit before provider I/O released the transaction-scoped lock.
    # Reacquire it before any normalized or Desk projection writes so the
    # lower-priority ingestion cron still cannot interleave with this phase.
    if release_transactions_for_network_io:
        await reacquire_writer_lock()

    with (
        telemetry.step("resolve_target_competitions")
        if telemetry is not None
        else nullcontext()
    ):
        competitions = await resolve_target_competitions(db, today=today)

    # Step 2 -- normalize (existing normalizer; best-effort per competition).
    normalized_ids: list[int] = []
    with telemetry.step("normalization") if telemetry is not None else nullcontext():
        for competition in competitions:
            assert competition.id is not None
            if await _normalize_competition(db, competition, raw_root=raw_root):
                normalized_ids.append(competition.id)

    # Step 2b -- scoped metrics rebuild (#523): refresh
    # summer_league_player_seasons for exactly the competitions normalize
    # touched this tick, so step 3's grading below reads fresh event
    # aggregates instead of stale ones. Writes are sequential (no concurrent
    # session use) and scoped by competition_id, so a competition this tick
    # didn't normalize -- including rows this module never wrote at all --
    # is never deleted or replaced. A no-op (empty `normalized_ids`, the
    # common case when raw fetch/audit hasn't produced anything new this
    # hour) skips the call entirely rather than issuing an empty-scope
    # rebuild.
    if normalized_ids:
        with (
            telemetry.step("scoped_metrics_rebuild")
            if telemetry is not None
            else nullcontext()
        ):
            await rebuild_sl_metrics(db, competition_ids=normalized_ids)

    mode: Literal["morning", "live"] = (
        "morning" if daily_state == EventDailyState.PREVIEW else "live"
    )

    graded_player_ids: list[int] = []
    storyline_results: dict[int, StorylineTickResult] = {}

    with telemetry.step("desk_projections") if telemetry is not None else nullcontext():
        for competition in competitions:
            assert competition.id is not None
            competition_id = competition.id

            # Step 3 -- grades (T2). Bulk-graded (#548): one batched pass for the
            # whole roster instead of a per-player `grade_player_event` loop --
            # players with no data/baseline are silently omitted (see
            # `grade_players_bulk`'s docstring), same skip semantics the old
            # per-player try/except performed.
            roster_player_ids = await _active_roster_player_ids(db, competition_id)
            grade_by_player: dict[int, GradeRow] = await grade_players_bulk(
                db, roster_player_ids, competition_id, baseline_version=baseline_version
            )
            graded_player_ids.extend(grade_by_player.keys())

            # Step 4 -- storylines (T3 + T4).
            result = await compute_desk_storylines(
                db,
                game_date=today,
                competition_id=competition_id,
                baseline_version=baseline_version,
                mode=mode,
            )
            storyline_results[competition_id] = result

            # Step 5 -- commentary (all eight #520 Facts onto T2 + grouped onto T4).
            await _commentary_for_competition(
                db,
                competition=competition,
                baseline_version=baseline_version,
                game_date=today,
                grade_by_player=grade_by_player,
                slate=result.slate,
            )

    # Step 6 -- render/state freshness: event_desk_state upsert (last;
    # reflects the freshly ingested scoreboard rather than the pre-tick
    # snapshot the step-0 pre-check saw, and is only reached once every
    # required step above has genuinely succeeded).
    with telemetry.step("event_desk_state") if telemetry is not None else nullcontext():
        states = await run_event_desk_tick(db, now=resolved_now, content_updated=True)

    # Step 7 -- render snapshot materialization (#551, launch-readiness item
    # 10). The FINAL step, only reached once every required step above has
    # genuinely succeeded -- builds and atomically upserts the complete
    # Preview/Live/Recap x Tracker cohort/stat-view variant matrix so the
    # homepage never has to reassemble the Desk at request time.
    with (
        telemetry.step("snapshot_materialization")
        if telemetry is not None
        else nullcontext()
    ):
        materialized_variant_count = await _materialize_render_snapshots(
            db, now=resolved_now
        )

    return DeskTickResult(
        now=resolved_now,
        executed_at=executed_at,
        dormant=False,
        daily_state=daily_state,
        content_updated=True,
        source_refreshed=not scoreboard_report.errors,
        source_advanced=(
            bool(normalized_ids)
            or bool(live_refresh_report.written)
            or bool(scoreboard_report.games_created)
            or bool(scoreboard_report.games_updated)
        ),
        baseline_version=baseline_version,
        scoreboard_report=scoreboard_report,
        live_refresh_report=live_refresh_report,
        normalized_competition_ids=tuple(normalized_ids),
        graded_player_ids=tuple(graded_player_ids),
        storyline_results=storyline_results,
        event_desk_states=tuple(states),
        bootstrapped=bootstrap_report is not None,
        materialized_variant_count=materialized_variant_count,
    )


def _summarize(result: DeskTickResult) -> str:
    """Human-readable one-tick summary for the CLI entrypoint."""
    if result.dormant:
        suffix = (
            " (bootstrap ingest attempted, still no anchor)"
            if result.bootstrapped
            else ""
        )
        return (
            f"Summer League Desk tick executed_at={result.executed_at.isoformat()} "
            f"effective_data_at={result.now.isoformat()}: off-window (dormant) -- "
            f"no-op content_updated=false{suffix}."
        )

    lines = [
        f"Summer League Desk tick executed_at={result.executed_at.isoformat()} "
        f"effective_data_at={result.now.isoformat()}: "
        f"daily_state={result.daily_state.value if result.daily_state else None} "
        f"baseline_version={result.baseline_version}"
        f"{' (#527 bootstrap ingest ran)' if result.bootstrapped else ''}",
        f"  content_updated={str(result.content_updated).lower()} "
        f"source_refreshed={str(result.source_refreshed).lower()} "
        f"source_advanced={str(result.source_advanced).lower()}",
        f"  graded_players={len(result.graded_player_ids)} "
        f"normalized_competitions={list(result.normalized_competition_ids)}",
        f"  materialized_render_snapshot_variants={result.materialized_variant_count}",
    ]
    if result.scoreboard_report is not None:
        report = result.scoreboard_report
        lines.append(
            f"  scoreboard: checked={report.competitions_checked} "
            f"created={report.games_created} updated={report.games_updated} "
            f"errors={report.errors} unresolved_team_ids={report.unresolved_team_ids}"
        )
    if result.live_refresh_report is not None:
        refresh = result.live_refresh_report
        lines.append(
            f"  live_refresh: selected={refresh.selected} groups={refresh.groups} "
            f"written={refresh.written} errors={refresh.errors}"
        )
    for competition_id, storyline_result in result.storyline_results.items():
        lines.append(
            f"  competition_id={competition_id}: slate_games={len(storyline_result.slate)} "
            f"quiet_hero={'yes' if storyline_result.quiet_slate_hero else 'no'}"
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> None:
    """Run one production tick without holding a transaction across NBA I/O."""
    now = datetime.fromisoformat(args.now) if args.now else None
    telemetry = PipelineTelemetry(job="desk", logger=logger)
    async with SessionLocal() as db:
        await start_pipeline(
            db,
            job=SummerLeaguePipelineJob.DESK,
            job_image=os.getenv("FLY_IMAGE_REF") or os.getenv("FLY_IMAGE"),
        )
        await db.commit()
        try:
            with telemetry.step("desk_tick"):
                result = await run_desk_tick(
                    db,
                    now=now,
                    raw_root=args.raw_root,
                    release_transactions_for_network_io=True,
                    telemetry=telemetry,
                )
            await complete_pipeline(
                db,
                job=SummerLeaguePipelineJob.DESK,
                metrics_rebuilt=bool(result.normalized_competition_ids),
                snapshots_materialized=bool(result.materialized_variant_count),
                source_refreshed=result.source_refreshed,
                source_advanced=result.source_advanced,
                projections_refreshed=result.content_updated,
                content_updated=result.content_updated,
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            try:
                async with db.begin():
                    await record_pipeline_failure(
                        db,
                        job=SummerLeaguePipelineJob.DESK,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
            except Exception:
                logger.exception("Could not record failed Summer League Desk tick")
            telemetry.finish("failed", content_updated=False)
            raise
    telemetry.finish(
        "succeeded",
        executed_at=result.executed_at.isoformat(),
        effective_data_at=result.now.isoformat(),
        content_updated=result.content_updated,
        source_refreshed=result.source_refreshed,
        source_advanced=result.source_advanced,
    )
    print(_summarize(result), flush=True)
    await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_RAW_ROOT,
        help="Root directory of audited raw Summer League snapshots.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "ISO-8601 datetime override for 'now' (manual reruns/backfills "
            "only); defaults to the current UTC instant."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and run one desk tick."""
    parser = build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

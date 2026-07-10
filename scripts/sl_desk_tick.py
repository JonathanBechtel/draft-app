"""Job B — the Summer League Desk hourly tick orchestrator.

`docs/plans/summer-league-scouts-desk-behavior-spec.md` §10 "Job B —
``sl_desk_tick``" pins the exact order this module wires together, every
step supplied by a sibling ticket's already-shipped, already-tested public
API — this module is pure orchestration, it implements no new grading,
storyline, or commentary logic of its own:

    0. schedule/scoreboard ingest (#515) — upsert today's + tomorrow's SL
       games (``tip_datetime`` / status) before anything else, since the
       state machine and Morning Card can't exist without tip times.
    1. (existing) normalize -- ``normalize_competition_games`` /
       ``normalize_player_game_logs`` (the same functions
       ``scripts/normalize_summer_league.py`` calls) pick up any newly
       audited raw box scores for today's competitions. Best-effort per
       competition: a competition with no audited raw run yet this hour is
       not an error (raw fetch/audit runs on its own cadence, independent
       of this hourly tick). Every competition normalize actually touched
       this tick is then passed to a *scoped* ``summer_league_player_seasons``
       rebuild (#523, ``app.services.summer_league.metrics.rebuild`` called
       with ``competition_ids=``) so step 2's grading reads freshly
       recomputed event aggregates rather than whatever was materialized on
       a prior tick -- never the unscoped, whole-table wipe-and-rebuild
       ``scripts/rebuild_sl_metrics.py`` performs (far too heavy for an
       hourly cron, and destructive to any competition this tick didn't
       just normalize).
    2. per active roster player -> grade vs the active T1 baseline (#503,
       ``desk_grades.grade_player_event``) -> T2.
    3. evaluate storyline triggers for today's games (#504,
       ``desk_storylines.compute_desk_storylines``) -> T3 + T4.
    4. commentary (#524) -- fire all eight #520 detectors for each graded
       player (``percentile``, ``cohort_rank``, ``streak``, ``self_delta``,
       ``leads_field``, ``debut_vs_bar``, ``count_club``, ``first_since``),
       each fed by a batched peer-population fetch from
       ``desk_fact_queries.py`` (never one query per player -- see that
       module's docstring for exactly how each is batched), and persist the
       resulting Facts onto T2 (#519 ``desk_commentary.persist_grade_facts``)
       and, grouped by tonight's rosters, onto each touched T4 slate row
       (``persist_slate_facts``) -- both of which run every fired Fact
       through Stage 2 selection (``desk_selection.dedup_facts`` /
       ``select_facts``, e.g. a rank-1 ``cohort_rank`` subsuming its own
       ``percentile``) via ``desk_commentary.build_facts_payload``.
    5. upsert ``event_desk_state`` (#506 ``event_desk.controller.
       run_event_desk_tick`` -- the only module that writes that table).

**Never rebuilds a distribution.** Job A (``scripts/build_sl_cohort_baselines.py``)
is the rare, offline cohort-baseline (T1) builder; this tick only ever reads
the currently active baseline version and fails loudly if none exists --
Job A must have run first.

**Off-window / dormant tick is inert.** Before touching the network or any
T2/T3/T4 table, the tick resolves the Summer League event's inner daily
state (Preview/Live/Recap) via the same pure state machine the framework
controller uses (`app.services.event_desk.state_machine.inner_state`).
When that resolves to ``None`` (the event's outer lifecycle phase isn't
``active`` -- Dormant, Announced, Warm-up, Wind-down, or Archived), steps
0-4 are skipped entirely and the tick only calls
``event_desk.controller.run_event_desk_tick`` once, which stamps the
freshness fields (``freshness_tick_at`` / ``next_tick_eta``) and leaves
``daily_state``/``hero_ref`` untouched -- "safe freshness behavior," per
spec, not a full state write.

**Idempotent.** Every write this module performs delegates to an existing
upsert (``grade_player_event``, ``compute_desk_storylines``,
``persist_grade_facts``, ``persist_slate_facts``, ``run_event_desk_tick``,
``upsert_scoreboard_games``) -- re-running the tick over the same data
updates rows in place rather than duplicating them.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/sl_desk_tick.py
  scripts/with-db-env.sh conda run -n draftguru python scripts/sl_desk_tick.py \
      --raw-root data/raw/nba_stats/summer_league
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.schemas.event_desk import EventDailyState, EventDeskState  # noqa: E402
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
from app.services.event_desk.registry import (  # noqa: E402
    SUMMER_LEAGUE_REGISTRATION,
    DeskEvent,
    WindowPriors,
)
from app.services.event_desk.state_machine import inner_state  # noqa: E402
from app.services.event_desk.timeutils import to_eastern_date  # noqa: E402
from app.services.summer_league.cohort_baselines import cohort_key_for  # noqa: E402
from app.services.summer_league.desk_commentary import (  # noqa: E402
    persist_grade_facts,
    persist_slate_facts,
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
from app.services.summer_league.desk_grades import GradeRow, grade_player_event  # noqa: E402
from app.services.summer_league.desk_storylines import (  # noqa: E402
    SlateRow,
    StorylineTickResult,
    compute_desk_storylines,
)
from app.services.summer_league.metrics import rebuild as rebuild_sl_metrics  # noqa: E402
from app.services.summer_league.nba_stats_client import NBAStatsClient  # noqa: E402
from app.services.summer_league.normalization import (  # noqa: E402
    normalize_competition_games,
    normalize_player_game_logs,
)
from app.services.summer_league.scoreboard_ingest import (  # noqa: E402
    ScoreboardIngestReport,
    resolve_target_competitions,
    run_scoreboard_ingest,
)
from app.utils.db_async import SessionLocal, engine  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_RAW_ROOT = Path("data/raw/nba_stats/summer_league")

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
    dormant: bool
    daily_state: Optional[EventDailyState]
    baseline_version: Optional[str] = None
    scoreboard_report: Optional[ScoreboardIngestReport] = None
    normalized_competition_ids: tuple[int, ...] = ()
    graded_player_ids: tuple[int, ...] = ()
    storyline_results: dict[int, StorylineTickResult] = field(default_factory=dict)
    event_desk_states: tuple[EventDeskState, ...] = ()


async def _resolve_daily_state(
    db: AsyncSession, *, now: datetime
) -> Optional[EventDailyState]:
    """Cheap pre-check: is the SL event's inner daily state resolvable right now?

    Mirrors the per-registration resolution
    ``app.services.event_desk.controller.run_event_desk_tick`` performs
    internally (that helper is private to the controller module) so this
    tick can decide, *before* touching the network or any T2/T3/T4 table,
    whether it's off-window (:func:`~app.services.event_desk.state_machine.inner_state`
    returns ``None``) and therefore inert. ``registration.sync`` is the same
    idempotent ``events`` row upsert the controller's own first step
    performs -- the only "write" this pre-check does.

    Args:
        db: Active database session.
        now: The tick's reference instant (naive UTC).

    Returns:
        The resolved daily state, or ``None`` when the event's outer
        lifecycle phase isn't ``active``.
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
    return inner_state(
        now, calendar_facts.today_schedule, calendar_facts.today_statuses, desk_event
    )


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
        await persist_grade_facts(
            db,
            player_id=player_id,
            competition_id=competition_id,
            baseline_version=baseline_version,
            facts=facts,
        )

    if not slate:
        return

    roster_by_game = await _game_roster_player_ids(
        db, competition_id=competition_id, game_date=game_date
    )
    for slate_row in slate:
        game_facts: list[Fact] = [
            fact
            for pid in roster_by_game.get(slate_row.game_id, [])
            for fact in fact_by_player.get(pid, [])
        ]
        await persist_slate_facts(
            db,
            game_id=slate_row.game_id,
            facts=game_facts,
            is_hero=slate_row.is_hero,
        )


def _season_range_start(season_range: str) -> int:
    """``"2017-2025"`` -> ``2017`` (mirrors ``cohort_baselines._parse_season_range``)."""
    start_str, _sep, _end_str = season_range.partition("-")
    return int(start_str)


async def run_desk_tick(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    raw_root: Path = DEFAULT_RAW_ROOT,
    client: Optional[NBAStatsClient] = None,
) -> DeskTickResult:
    """Job B -- the Summer League Desk hourly tick (module docstring has the full order).

    Does not commit; the caller controls the transaction (mirrors every
    other Summer League ingest/tick step in this repo -- ``main`` below
    wraps this in ``async with db.begin()``, matching
    ``scripts/build_sl_cohort_baselines.py``).

    Args:
        db: Active database session (caller controls the transaction).
        now: Override for "now" (tests only); defaults to the current UTC
            instant.
        raw_root: Root directory of audited raw Summer League snapshots,
            forwarded to the normalize step.
        client: Optional injected :class:`NBAStatsClient` (tests only),
            forwarded to the scoreboard ingest step; when omitted a real
            client is opened for the duration of that step.

    Returns:
        A :class:`DeskTickResult` summarizing every stage's outcome.

    Raises:
        RuntimeError: The tick is not off-window (there's real work to do)
            but no active T1 cohort baseline exists -- Job A
            (``scripts/build_sl_cohort_baselines.py``) must run first.
    """
    resolved_now = now if now is not None else datetime.utcnow()

    daily_state = await _resolve_daily_state(db, now=resolved_now)
    if daily_state is None:
        # Off-window/dormant: inert. `run_event_desk_tick` still upserts
        # event_desk_state (phase + freshness stamp), but every network call
        # and every T2/T3/T4 write below is skipped entirely.
        states = await run_event_desk_tick(db, now=resolved_now)
        return DeskTickResult(
            now=resolved_now,
            dormant=True,
            daily_state=None,
            event_desk_states=tuple(states),
        )

    baseline_version = await _active_baseline_version(db)
    if baseline_version is None:
        raise RuntimeError(
            "No active Summer League cohort baseline (T1) found -- run "
            "scripts/build_sl_cohort_baselines.py before the desk tick."
        )

    today = to_eastern_date(resolved_now)

    # Step 0 -- schedule/scoreboard ingest.
    scoreboard_report = await run_scoreboard_ingest(db, today=today, client=client)

    competitions = await resolve_target_competitions(db, today=today)

    # Step 1 -- normalize (existing normalizer; best-effort per competition).
    normalized_ids: list[int] = []
    for competition in competitions:
        assert competition.id is not None
        if await _normalize_competition(db, competition, raw_root=raw_root):
            normalized_ids.append(competition.id)

    # Step 1b -- scoped metrics rebuild (#523): refresh
    # summer_league_player_seasons for exactly the competitions normalize
    # touched this tick, so step 2's grading below reads fresh event
    # aggregates instead of stale ones. Writes are sequential (no concurrent
    # session use) and scoped by competition_id, so a competition this tick
    # didn't normalize -- including rows this module never wrote at all --
    # is never deleted or replaced. A no-op (empty `normalized_ids`, the
    # common case when raw fetch/audit hasn't produced anything new this
    # hour) skips the call entirely rather than issuing an empty-scope
    # rebuild.
    if normalized_ids:
        await rebuild_sl_metrics(db, competition_ids=normalized_ids)

    mode: Literal["morning", "live"] = (
        "morning" if daily_state == EventDailyState.PREVIEW else "live"
    )

    graded_player_ids: list[int] = []
    storyline_results: dict[int, StorylineTickResult] = {}

    for competition in competitions:
        assert competition.id is not None
        competition_id = competition.id

        # Step 2 -- grades (T2).
        grade_by_player: dict[int, GradeRow] = {}
        for player_id in await _active_roster_player_ids(db, competition_id):
            try:
                grade_by_player[player_id] = await grade_player_event(
                    db, player_id, competition_id, baseline_version=baseline_version
                )
            except ValueError as exc:
                logger.info(
                    "sl_desk_tick: skip grading player_id=%s competition_id=%s (%s)",
                    player_id,
                    competition_id,
                    exc,
                )
        graded_player_ids.extend(grade_by_player.keys())

        # Step 3 -- storylines (T3 + T4).
        result = await compute_desk_storylines(
            db,
            game_date=today,
            competition_id=competition_id,
            baseline_version=baseline_version,
            mode=mode,
        )
        storyline_results[competition_id] = result

        # Step 4 -- commentary (all eight #520 Facts onto T2 + grouped onto T4).
        await _commentary_for_competition(
            db,
            competition=competition,
            baseline_version=baseline_version,
            game_date=today,
            grade_by_player=grade_by_player,
            slate=result.slate,
        )

    # Step 5 -- event_desk_state upsert (last; reflects the freshly ingested
    # scoreboard rather than the pre-tick snapshot the step-0 pre-check saw).
    states = await run_event_desk_tick(db, now=resolved_now)

    return DeskTickResult(
        now=resolved_now,
        dormant=False,
        daily_state=daily_state,
        baseline_version=baseline_version,
        scoreboard_report=scoreboard_report,
        normalized_competition_ids=tuple(normalized_ids),
        graded_player_ids=tuple(graded_player_ids),
        storyline_results=storyline_results,
        event_desk_states=tuple(states),
    )


def _summarize(result: DeskTickResult) -> str:
    """Human-readable one-tick summary for the CLI entrypoint."""
    if result.dormant:
        return f"Summer League Desk tick @ {result.now.isoformat()}: off-window (dormant) -- no-op."

    lines = [
        f"Summer League Desk tick @ {result.now.isoformat()}: "
        f"daily_state={result.daily_state.value if result.daily_state else None} "
        f"baseline_version={result.baseline_version}",
        f"  graded_players={len(result.graded_player_ids)} "
        f"normalized_competitions={list(result.normalized_competition_ids)}",
    ]
    if result.scoreboard_report is not None:
        report = result.scoreboard_report
        lines.append(
            f"  scoreboard: checked={report.competitions_checked} "
            f"created={report.games_created} updated={report.games_updated} "
            f"errors={report.errors}"
        )
    for competition_id, storyline_result in result.storyline_results.items():
        lines.append(
            f"  competition_id={competition_id}: slate_games={len(storyline_result.slate)} "
            f"quiet_hero={'yes' if storyline_result.quiet_slate_hero else 'no'}"
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> None:
    """Open a session, run one tick inside a transaction, and print a summary."""
    now = datetime.fromisoformat(args.now) if args.now else None
    async with SessionLocal() as db:
        async with db.begin():
            result = await run_desk_tick(db, now=now, raw_root=args.raw_root)
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

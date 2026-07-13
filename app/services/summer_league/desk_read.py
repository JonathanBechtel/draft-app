"""Summer League Desk read service — assembles one `/` render from T2/T4 (#508).

The home route calls :func:`get_desk_view` exactly once -- the ONE service call
that assembles both the payload (:func:`get_desk_payload`) and its player/team
view-context enrichment (:func:`get_desk_view_context`) for `/`'s template. Both
are **pure reads**: no writes, no distribution rebuilds, no per-request
storyline/grade recompute. Two different things happen at two different times,
and this module is careful never to conflate them:

* **Content** (hero copy, slate weights, grades, commentary prose) is whatever the
  last hourly tick (`scripts/sl_desk_tick.py`, #516/#523) wrote to T2
  (`summer_league_desk_player_grades`) / T4 (`summer_league_desk_slate`). This can
  legitimately lag by up to an hour — that's what the `freshness` stamp on the
  payload communicates.
* **State** (`daily_state` — Morning Card / Live Desk / The Ledger, plus whether
  the window is open at all) is **resolved fresh on every call** by the framework's
  pure resolvers (`app.services.event_desk.lifecycle.lifecycle_phase`,
  `app.services.event_desk.state_machine.inner_state`) over `(now, today's tip
  schedule, today's game statuses)`. `event_desk_state` is read here **only** for
  its freshness stamp (`freshness_tick_at` / `next_tick_eta`) — its own
  `daily_state`/`lifecycle_phase` columns are never read back as the verdict. This
  is the behavior spec §2 "Resolution & data prerequisites" contract: a 7:05pm tip
  must render Live at 7:06 even if the last tick ran at 7:00 and last saw every
  game `scheduled`; the Ledger→Morning flip must fire at its computed minute, not
  the next hourly tick. See `state_machine.inner_state`'s docstring for the
  "scheduled-tip fallback" that makes this true even on a stale tick.

**Off-window.** When the event's outer lifecycle phase isn't `active`
(Dormant/Announced/Warm-up/Wind-down/Archived) — including when the `events` row
doesn't exist yet at all, i.e. no tick has ever run — this function returns
``None``. The `/` route's job (not this module's) is to render the behavior
spec §2 "collapses to a single archive strip" treatment in that case; the Desk
states UI ticket (#509) owns that template. Off-window is intentionally the
*cheapest* path: a single `events` lookup short-circuits everything else.

**Quiet slate.** Per behavior spec §4, the front page must never have a dead
hero: when today's slate is empty or nothing on it clears a storyline threshold,
the hero promotes the class leader to date. This reuses
`app.services.summer_league.desk_storylines.select_quiet_slate_hero` (the pure
ranking rule) and `slate_needs_quiet_fallback` (the trigger predicate) rather
than reimplementing either.

**No per-player, no per-game queries.** Every section is assembled from a small,
fixed number of `.in_()`-batched queries over the slate/roster in view — see each
helper's docstring for exactly what it fetches. Query count does not grow with
the number of tracked players or games; it's bounded by the number of *sections*
this call assembles.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace as dataclass_replace
from datetime import date, datetime, timezone
from typing import Callable, Mapping, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.event_desk import (
    Event,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
)
from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
    SummerLeagueDeskStoryline,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.event_desk.controller import TICK_INTERVAL
from app.services.event_desk.lifecycle import lifecycle_phase, resolve_home_owner
from app.services.event_desk.payload import (
    DeskFreshness,
    DeskHero,
    DeskHeroLine,
    DeskLedgerRow,
    DeskLiveBoardRow,
    DeskPayload,
    DeskSlateRow,
    DeskTrackerRow,
    DeskTrackerSection,
)
from app.services.event_desk.registry import (
    DeskEvent,
    WindowPriors,
    calendar_facts_for_competition_ids,
)
from app.services.event_desk.state_machine import inner_state
from app.services.event_desk.timeutils import to_eastern, to_eastern_date
from app.services.summer_league.cohort_baselines import (
    blend_event_aggregates,
    cohort_key_for,
)
from app.services.summer_league.desk_grades import (
    grade_for_percentile,
    percentile_of_value,
)
from app.services.summer_league.desk_selection import Surface
from app.services.summer_league.constants import MINUTES_PER_GAME
from app.services.summer_league.desk_storylines import (
    ClassLeaderCandidate,
    draft_slot_fallback,
    select_quiet_slate_hero,
    slate_needs_quiet_fallback,
)
from app.services.summer_league.metrics import game_score_from_row
from app.services.summer_league.scoreboard_ingest import EVENT_KEY_SUMMER_LEAGUE
from app.services.summer_league.team_logos import franchise_logo_url
from app.services.summer_league_explorer_service import (
    rollup_rate_composite,
    rollup_recombinable,
)
from app.utils.images import get_placeholder_url, get_player_image_url

# Roster statuses treated as "actively tracked" -- mirrors
# `desk_storylines._ROSTER_ACTIVE_STATUSES` / `sl_desk_tick._ROSTER_ACTIVE_STATUSES`.
_ROSTER_ACTIVE_STATUSES = {
    AffiliationStatus.ANNOUNCED,
    AffiliationStatus.CONFIRMED,
    AffiliationStatus.ACTIVE,
}

# Class Tracker defaults (behavior spec §7). The toggle UI (#511) round-trips
# `tracker_cohort`/`tracker_stat_view` from query params; this module just
# validates and falls back to these when unset/unknown.
DEFAULT_TRACKER_COHORT = "full_class"
DEFAULT_TRACKER_STAT_VIEW = "box"
TRACKER_COHORTS: tuple[str, ...] = (
    "lottery",
    "round1",
    "round2",
    "full_class",
    "sophomores",
    "undrafted",
)
TRACKER_STAT_VIEWS: tuple[str, ...] = ("box", "per36", "per100", "advanced")
TRACKER_CAP = 30

# The Ledger shows a bounded top-performers list, not the full field.
MAX_LEDGER_ROWS = 10

# Freshness is "stale" once it's older than this cadence multiple. Twice the
# controller's hourly tick (`TICK_INTERVAL`) tolerates exactly one missed
# tick before flagging staleness -- the same documented policy
# `scripts/check_sl_desk_readiness.py::DEFAULT_STALENESS_HOURS` uses for its
# post-tick readiness gate, reused here (not re-derived) so the two never
# drift on what "stale" means.
FRESHNESS_STALE_AFTER = 2 * TICK_INTERVAL

# Lifecycle phases the Desk can take over the home page for (framework doc
# "EventDesk controller": home-eligible = Warm-up/Active/Wind-down; V1 has no
# Warm-up content yet -- behavior spec §9 "Pre-roll ... P2" -- so Warm-up
# still collapses to the off-window strip here until that ships). Wind-down
# renders the Recap treatment straight through (behavior spec/framework doc:
# "Wind-down: last final + post_roll_days tail -- final recap persists").
_HOME_TAKEOVER_PHASES = (EventLifecyclePhase.ACTIVE, EventLifecyclePhase.WINDDOWN)

_FALLBACK_HERO = DeskHero(
    kind="quiet_slate",
    game_id=None,
    subject_player_id=None,
    subject_player_id_2=None,
    headline="Summer League coverage begins once tonight's rosters are set.",
    tagline=None,
    facts=[],
)


# --------------------------------------------------------------------------- #
# Small, generic helpers
# --------------------------------------------------------------------------- #
def _extract_prose(
    facts: Optional[Sequence[dict[str, object]]], *, surface: Surface
) -> Optional[str]:
    """Pull one rendered prose string off a persisted T2/T4 ``facts`` payload.

    Reads the tick-time-rendered ``"prose"``/``"selected_for"`` entries built by
    `desk_commentary.build_facts_payload` -- never re-renders anything. Prefers
    an entry actually selected for ``surface``; falls back to any rendered
    prose (e.g. a hero row lacking a dedicated ``hero_tagline`` selection still
    reads a ``tick_note``) rather than surfacing nothing.

    Args:
        facts: A T2/T4 row's ``facts`` JSONB column (may be ``None``/empty).
        surface: The preferred `desk_selection.Surface` to read for.

    Returns:
        The first matching rendered sentence, or ``None`` when nothing rendered.
    """
    if not facts:
        return None
    for entry in facts:
        prose = entry.get("prose")
        selected_for = entry.get("selected_for")
        surfaces = selected_for if isinstance(selected_for, (list, tuple, set)) else ()
        if prose and surface.value in surfaces:
            return str(prose)
    for entry in facts:
        prose = entry.get("prose")
        if prose:
            return str(prose)
    return None


def _team_label(entry: Optional[SummerLeagueTeamEntry]) -> str:
    """Short display label for a team entry: abbreviation, falling back to name."""
    if entry is None:
        return "TBD"
    return entry.raw_team_abbreviation or entry.raw_team_name or "TBD"


def _matchup_label(
    game: Optional[SummerLeagueGame], teams: dict[int, SummerLeagueTeamEntry]
) -> str:
    """``"AWY @ HOM"`` matchup label from a game + its batch-fetched team entries."""
    if game is None:
        return "TBD"
    home = _team_label(teams.get(game.home_team_entry_id or -1))
    away = _team_label(teams.get(game.away_team_entry_id or -1))
    return f"{away} @ {home}"


async def _active_baseline_version(db: AsyncSession) -> Optional[str]:
    """The currently active T1 ``baseline_version``, or ``None`` if Job A hasn't run.

    A missing baseline degrades gracefully throughout this module (percentiles/
    grades render as ``None``/em-dash) rather than raising -- the Desk must still
    render slate/live-board/ledger structure even before Job A's first run.
    """
    stmt = (
        select(SummerLeagueCohortBaseline.baseline_version)  # type: ignore[call-overload]
        .where(SummerLeagueCohortBaseline.is_active.is_(True))  # type: ignore[attr-defined]
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


async def _fetch_games(
    db: AsyncSession, game_ids: Sequence[int]
) -> dict[int, SummerLeagueGame]:
    """Batch-fetch every game in ``game_ids`` -- one query regardless of count."""
    if not game_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(SummerLeagueGame).where(
                    SummerLeagueGame.id.in_(game_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    return {g.id: g for g in rows if g.id is not None}


async def _fetch_team_entries(
    db: AsyncSession, team_entry_ids: Sequence[Optional[int]]
) -> dict[int, SummerLeagueTeamEntry]:
    """Batch-fetch every team entry referenced -- one query regardless of count."""
    ids = sorted({tid for tid in team_entry_ids if tid is not None})
    if not ids:
        return {}
    rows = (
        (
            await db.execute(
                select(SummerLeagueTeamEntry).where(
                    SummerLeagueTeamEntry.id.in_(ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    return {t.id: t for t in rows if t.id is not None}


def _et_time(dt: datetime) -> str:
    """``"4:12pm ET"``-style clock stamp for a naive-UTC (or aware) instant."""
    eastern = to_eastern(dt)
    hour12 = eastern.hour % 12 or 12
    ampm = "am" if eastern.hour < 12 else "pm"
    return f"{hour12}:{eastern.minute:02d}{ampm} ET"


def _et_label(dt: datetime) -> str:
    """``"as of 4:12pm ET"``-style stamp for a naive-UTC (or aware) instant."""
    return f"as of {_et_time(dt)}"


# --------------------------------------------------------------------------- #
# Window resolution (the request-time state contract)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _WindowState:
    """The resolved outer/inner state for one :func:`get_desk_payload` call."""

    event_row: Event
    daily_state: EventDailyState
    competition_ids: list[int]
    is_home_owner: bool


def _competition_ids_from_calendar_ref(event_row: Event) -> list[int]:
    """Read ``calendar_ref["competition_ids"]`` off a synced ``events`` row."""
    raw_ids = event_row.calendar_ref.get("competition_ids")
    if isinstance(raw_ids, list) and raw_ids:
        return [int(x) for x in raw_ids]
    return []


async def _resolve_window_state(
    db: AsyncSession, *, now: datetime, require_owner: bool = True
) -> Optional[_WindowState]:
    """Resolve the SL event's current window state, fresh, at ``now``.

    Returns ``None`` whenever the outer lifecycle phase isn't one of
    :data:`_HOME_TAKEOVER_PHASES` -- including when no ``events`` row exists
    yet (the tick has never run). This is the off-window signal the caller
    uses to render the collapsed strip instead of the full Desk. Never writes
    anything (unlike `registry.sync_summer_league_event`, which this
    deliberately does NOT call).

    Wind-down renders the Recap treatment straight through to Archived
    (framework doc: "final recap persists") -- the inner Preview/Live/Recap
    machine only runs while Active (`inner_state` requires it and returns
    `None` otherwise), so Wind-down's ``daily_state`` is set directly to
    ``RECAP`` here rather than calling `inner_state`.

    Args:
        db: Active database session.
        now: The request instant (naive UTC).
        require_owner: Whether the resolved event must also be the winning
            `is_home_owner` (single-owner-by-priority) to take over the home
            page -- ``True`` in auto mode (`settings.sl_desk_force_mode ==
            "auto"`). ``settings.sl_desk_force_mode == "on"`` passes
            ``False`` to bypass just this gate; it never fabricates an
            in-window phase that the calendar doesn't have.

    Returns:
        The resolved window state, or ``None`` when off-window (or, with
        ``require_owner=True``, when in-window but not the home owner).

    **Query budget (#548).** Exactly 3 queries in the common case (an
    ``events`` row already carries ``calendar_ref["competition_ids"]``, the
    normal post-tick state): the ``events`` lookup, then
    `registry.calendar_facts_for_competition_ids`'s two calendar reads
    (game_dates + today's schedule/statuses). This resolves
    ``competition_ids`` ONCE, up front, straight off the already-fetched
    ``event_row`` -- the pre-#548 version called the full
    `registry.resolve_calendar_facts` (which redundantly re-fetches the SAME
    ``events`` row plus every competition row via its own
    `~app.services.summer_league.scoreboard_ingest.resolve_target_competitions`
    call) and only derived ``competition_ids`` a second time afterward, for a
    total of 5. The rare fallback (an ``events`` row with an empty
    ``calendar_ref`` -- Job B's registration sync never wrote one) adds one
    more query, matching the pre-#548 fallback path's cost exactly.
    """
    event_stmt = select(Event).where(Event.key == EVENT_KEY_SUMMER_LEAGUE)  # type: ignore[arg-type]
    event_row = (await db.execute(event_stmt)).scalar_one_or_none()
    if event_row is None:
        return None

    competition_ids = _competition_ids_from_calendar_ref(event_row)
    if not competition_ids:
        year = to_eastern_date(now).year
        comp_ids = (
            (
                await db.execute(
                    select(SummerLeagueCompetition.id).where(  # type: ignore[call-overload]
                        SummerLeagueCompetition.year == year  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )
        competition_ids = [cid for cid in comp_ids if cid is not None]

    today = to_eastern_date(now)
    calendar_facts = await calendar_facts_for_competition_ids(
        db, competition_ids=competition_ids, today=today
    )
    desk_event = DeskEvent(
        key=EVENT_KEY_SUMMER_LEAGUE,
        priority=event_row.priority,
        window_priors=WindowPriors.from_dict(event_row.window_priors),
        game_dates=calendar_facts.game_dates,
    )
    phase = lifecycle_phase(now, desk_event)
    if phase not in _HOME_TAKEOVER_PHASES:
        return None

    if phase == EventLifecyclePhase.ACTIVE:
        daily_state = inner_state(
            now,
            calendar_facts.today_schedule,
            calendar_facts.today_statuses,
            desk_event,
        )
        # Guaranteed non-None: `inner_state` only returns None when the outer
        # phase isn't ACTIVE, which is already ruled out above.
        assert daily_state is not None
    else:
        daily_state = EventDailyState.RECAP

    owner = resolve_home_owner(now, [desk_event])
    is_home_owner = owner is not None and owner.key == desk_event.key
    if require_owner and not is_home_owner:
        return None

    return _WindowState(
        event_row=event_row,
        daily_state=daily_state,
        competition_ids=competition_ids,
        is_home_owner=is_home_owner,
    )


def _build_freshness(
    *,
    last_tick_at: Optional[datetime],
    next_tick_eta: Optional[datetime],
    now: datetime,
) -> DeskFreshness:
    """Pure freshness verdict from a raw ``(tick_at, next_tick_eta)`` stamp -- no I/O.

    Never fabricates "as of now": a missing stamp (``last_tick_at is None`` --
    no tick has ever run for this event) renders ``state="missing"`` with an
    explicit "unavailable" label, never `_et_label(now)`. A stamp older than
    :data:`FRESHNESS_STALE_AFTER` renders ``state="stale"`` with an honest
    "-- stale" suffix rather than silently presenting it as current. The
    next-tick ETA is pre-rendered once here (``next_tick_eta_label``) so the
    one template slot that shows it never has to duplicate the "as of" stamp
    or do its own timezone math.

    Shared by two callers that source the SAME kind of stamp from two
    different places: :func:`_freshness_for` (reads it fresh from
    ``event_desk_state`` for a live, per-request assembly) and the
    snapshot-backed request-time read (`get_desk_view_from_snapshot`, which
    reads the identical stamp already copied onto
    ``EventDeskRenderSnapshot.source_freshness_tick_at`` /
    ``source_freshness_next_tick_eta`` at materialization time -- see that
    table's module docstring). This is why staleness must be computed HERE,
    against the caller's ``now``, rather than trusted verbatim off a
    persisted ``DeskPayload.freshness`` -- a snapshot's embedded freshness was
    computed relative to the *tick's own* "now" at write time (always
    "fresh", by construction, relative to itself), so a reader has to
    re-judge it against the CURRENT request's "now" or a cron outage would
    never surface as stale.

    Args:
        last_tick_at: The last successful tick's timestamp, or ``None`` if no
            tick has ever run.
        next_tick_eta: The next tick's ETA, or ``None``.
        now: The instant to judge staleness against (the live request's
            "now", not necessarily the tick's own "now").

    Returns:
        The honest :class:`DeskFreshness` verdict.
    """
    if last_tick_at is None:
        return DeskFreshness(
            last_tick_at=None,
            next_tick_eta=next_tick_eta,
            as_of_et_label="freshness unavailable -- no tick has run yet",
            state="missing",
            next_tick_eta_label=None,
        )

    is_stale = (now - last_tick_at) > FRESHNESS_STALE_AFTER
    as_of_et_label = _et_label(last_tick_at) + (" -- stale" if is_stale else "")
    next_tick_eta_label = (
        f"next update ~{_et_time(next_tick_eta)}" if next_tick_eta is not None else None
    )
    return DeskFreshness(
        last_tick_at=last_tick_at,
        next_tick_eta=next_tick_eta,
        as_of_et_label=as_of_et_label,
        state="stale" if is_stale else "fresh",
        next_tick_eta_label=next_tick_eta_label,
    )


async def _freshness_for(
    db: AsyncSession, event_row: Event, *, now: datetime
) -> DeskFreshness:
    """Read ``event_desk_state``'s stamp and build the honest freshness verdict.

    Thin DB-backed wrapper around :func:`_build_freshness` -- see that
    function's docstring for the actual staleness logic.
    """
    stmt = select(EventDeskState).where(EventDeskState.event_id == event_row.id)  # type: ignore[arg-type]
    state_row = (await db.execute(stmt)).scalar_one_or_none()
    last_tick_at = state_row.freshness_tick_at if state_row else None
    next_tick_eta = state_row.next_tick_eta if state_row else None
    return _build_freshness(
        last_tick_at=last_tick_at, next_tick_eta=next_tick_eta, now=now
    )


# --------------------------------------------------------------------------- #
# Slate / hero (Morning Card + Live Desk share this shape; behavior spec §5)
# --------------------------------------------------------------------------- #
async def _fetch_today_slate(
    db: AsyncSession, *, competition_ids: Sequence[int], today: date
) -> list[SummerLeagueDeskSlate]:
    """Today's T4 rows for the event cluster, ranked -- rank 1 is the hero."""
    if not competition_ids:
        return []
    stmt = (
        select(SummerLeagueDeskSlate)
        .where(
            SummerLeagueDeskSlate.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
            SummerLeagueDeskSlate.game_date == today,  # type: ignore[arg-type]
        )
        .order_by(SummerLeagueDeskSlate.rank.asc())  # type: ignore[attr-defined]
    )
    return list((await db.execute(stmt)).scalars().all())


def _pick_hero_slate_row(
    slate_rows: Sequence[SummerLeagueDeskSlate],
    games: dict[int, SummerLeagueGame],
    *,
    live: bool,
) -> Optional[SummerLeagueDeskSlate]:
    """Pick the hero game (behavior spec §4).

    Live re-selects each call: the highest-weighted game currently
    ``in_progress`` wins, even if the tick-computed ``is_hero`` flag points at a
    game that has since gone final -- "re-selected each tick -- if the marquee
    ended, a live game takes over." Falls back to the tick's own ``is_hero``/
    top-ranked row when nothing is currently in progress (e.g. between games).
    """
    if not slate_rows:
        return None
    pool = slate_rows
    if live:
        in_progress = [
            row
            for row in slate_rows
            if (game := games.get(row.game_id)) is not None
            and game.status == SummerLeagueGameStatus.IN_PROGRESS
        ]
        if in_progress:
            pool = in_progress
    for row in pool:
        if row.is_hero:
            return row
    return min(pool, key=lambda row: row.rank)


def _build_slate_rows(
    slate_rows: Sequence[SummerLeagueDeskSlate],
    games: dict[int, SummerLeagueGame],
    teams: dict[int, SummerLeagueTeamEntry],
    *,
    exclude_game_id: Optional[int],
) -> list[DeskSlateRow]:
    """The Rest of Tonight's Slate: every game minus the hero (behavior spec §5)."""
    out: list[DeskSlateRow] = []
    for row in slate_rows:
        if row.game_id == exclude_game_id:
            continue
        game = games.get(row.game_id)
        out.append(
            DeskSlateRow(
                game_id=row.game_id,
                matchup_label=_matchup_label(game, teams),
                status=game.status.value if game else "unknown",
                tip_datetime=game.tip_datetime if game else None,
                weight=row.total_weight,
                read=_extract_prose(row.facts, surface=Surface.TICK_NOTE),
            )
        )
    return out


async def _hero_subjects_for_game(
    db: AsyncSession, game_id: int
) -> tuple[Optional[int], Optional[int]]:
    """The hero game's top-weighted storyline's subject(s), for the hero's ``subject_player_id*``."""
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeagueDeskStoryline.subject_player_id,
            SummerLeagueDeskStoryline.subject_player_id_2,
        )
        .where(SummerLeagueDeskStoryline.game_id == game_id)  # type: ignore[arg-type]
        .order_by(SummerLeagueDeskStoryline.weight.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None, None
    return row[0], row[1]


def _hero_line_from_logs(
    logs_by_game: Mapping[int, Sequence[SummerLeaguePlayerGameLog]],
    *,
    game_id: Optional[int],
    player_id: Optional[int],
) -> Optional[DeskHeroLine]:
    """One Live hero subject's tonight's running box line (#541).

    ``None`` only when there's no subject at all (``player_id is None`` --
    e.g. a single-subject Live hero's ``subject_line_2``). A real subject who
    simply hasn't logged tonight's game yet (pretip) still gets a
    :class:`DeskHeroLine`, just with every field ``None`` -- the template
    renders that as an em dash, never a zero or an event/career total.

    Args:
        logs_by_game: Every resolved box-score line for tonight's slate,
            grouped by ``game_id`` (see :func:`_fetch_game_logs_for_games`).
        game_id: The hero game's id (``None`` degrades to the all-``None``
            line, same as an unresolved subject).
        player_id: The subject's player id, or ``None``.

    Returns:
        The subject's tonight line, or an all-``None`` line pre-tip.
    """
    if player_id is None:
        return None
    if game_id is not None:
        for row in logs_by_game.get(game_id, []):
            if row.player_id == player_id:
                return DeskHeroLine(
                    pts=row.pts,
                    reb=row.reb,
                    ast=row.ast,
                    gmsc=round(game_score_from_row(row), 2),
                )
    return DeskHeroLine(pts=None, reb=None, ast=None, gmsc=None)


async def _build_game_hero(
    db: AsyncSession,
    hero_row: SummerLeagueDeskSlate,
    games: dict[int, SummerLeagueGame],
    teams: dict[int, SummerLeagueTeamEntry],
    *,
    kind: str,
    logs_by_game: Optional[Mapping[int, Sequence[SummerLeaguePlayerGameLog]]] = None,
) -> DeskHero:
    """Build the Morning marquee / Live key-matchup hero from a T4 row.

    ``logs_by_game`` (#541) only matters for the Live hero (``kind ==
    "live_duel"``) -- Morning's marquee never populates ``subject_line*``
    since nothing has tipped yet. Callers pass the SAME batched
    :func:`_fetch_game_logs_for_games` result :func:`_build_live_board` uses,
    so adding both subjects' running lines costs zero additional queries over
    what the Live Desk's top-performer board already fetched.
    """
    game = games.get(hero_row.game_id)
    matchup = _matchup_label(game, teams)
    subject1, subject2 = await _hero_subjects_for_game(db, hero_row.game_id)
    headline = _extract_prose(hero_row.facts, surface=Surface.HERO_TAGLINE) or (
        f"Tonight's top storyline: {matchup}"
    )
    tagline = _extract_prose(hero_row.facts, surface=Surface.TICK_NOTE)
    if tagline == headline:
        tagline = None

    subject_line: Optional[DeskHeroLine] = None
    subject_line_2: Optional[DeskHeroLine] = None
    if kind == "live_duel":
        logs = logs_by_game or {}
        game_logs = list(logs.get(hero_row.game_id, []))
        logged_ids = {row.player_id for row in game_logs}
        played_ids = {row.player_id for row in game_logs if _played(row)}

        def _is_dnp_shell(player_id: Optional[int]) -> bool:
            # A CONFIRMED non-participant: the subject has a box row for
            # tonight's game but it shows no minutes (a DNP roster shell -- the
            # rostered veteran who dressed but sat, e.g. Cam Reddish). A subject
            # with NO row yet is merely pre-tip: keep them, their line renders
            # em-dash until they log (#541), never dropped.
            return (
                player_id is not None
                and player_id in logged_ids
                and player_id not in played_ids
            )

        # A DNP-shell subject must not headline the LIVE key matchup with an
        # em-dash line. Fall back to the game's real top performer (dropping the
        # now-orphaned duel partner); a DNP-shell duel partner is likewise
        # dropped. Pre-tip subjects (no row yet) are left untouched.
        if _is_dnp_shell(subject1):
            top = _top_performers_from_logs({hero_row.game_id: game_logs}).get(
                hero_row.game_id
            )
            if top is not None:
                subject1 = top[0]
                subject2 = None
        if _is_dnp_shell(subject2):
            subject2 = None
        subject_line = _hero_line_from_logs(
            logs, game_id=hero_row.game_id, player_id=subject1
        )
        subject_line_2 = _hero_line_from_logs(
            logs, game_id=hero_row.game_id, player_id=subject2
        )

    return DeskHero(
        kind=kind,
        game_id=hero_row.game_id,
        subject_player_id=subject1,
        subject_player_id_2=subject2,
        headline=headline,
        tagline=tagline,
        facts=list(hero_row.facts or []),
        subject_line=subject_line,
        subject_line_2=subject_line_2,
    )


# --------------------------------------------------------------------------- #
# Quiet-slate fallback hero (reuses desk_storylines' pure ranking rule)
# --------------------------------------------------------------------------- #
async def _quiet_slate_hero(
    db: AsyncSession, *, competition_ids: Sequence[int], baseline_version: Optional[str]
) -> Optional[DeskHero]:
    """The class-leader fallback hero (behavior spec §4 "always force a headline").

    Fetches every T2 grade across the event cluster's competitions (batched,
    not per-player) and hands them to
    `desk_storylines.select_quiet_slate_hero` -- the same pure ranking rule the
    tick-time orchestrator uses (`select_quiet_slate_hero_from_grades`), just
    resolved across every competition in the cluster rather than one.
    """
    if not competition_ids or baseline_version is None:
        return None
    stmt = select(SummerLeagueDeskPlayerGrade).where(
        SummerLeagueDeskPlayerGrade.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
        SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
    )
    grade_rows = (await db.execute(stmt)).scalars().all()
    if not grade_rows:
        return None

    player_ids = [g.player_id for g in grade_rows]
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
    label_by_id = {p.id: (p.display_name or f"Player {p.id}") for p in players}

    candidates = [
        ClassLeaderCandidate(
            player_id=g.player_id,
            player_label=label_by_id.get(g.player_id, f"Player {g.player_id}"),
            pctl=g.pctl,
            gmsc=g.subject_value,
            gated=g.gated,
        )
        for g in grade_rows
    ]
    winner = select_quiet_slate_hero(candidates)
    if winner is None:
        return None

    winner_facts = next(
        (g.facts for g in grade_rows if g.player_id == winner.player_id), None
    )
    headline = _extract_prose(winner_facts, surface=Surface.LEDGER_ECHO) or (
        f"{winner.player_label} still leads the class at {winner.gmsc:g} GmSc."
    )
    tagline = _extract_prose(winner_facts, surface=Surface.TICK_NOTE)
    if tagline == headline:
        tagline = None
    return DeskHero(
        kind="quiet_slate",
        game_id=None,
        subject_player_id=winner.player_id,
        subject_player_id_2=None,
        headline=headline,
        tagline=tagline,
        facts=list(winner_facts or []),
    )


# --------------------------------------------------------------------------- #
# Live Desk's all-games board (behavior spec §1 "Live tick board") + the Live
# hero's two-subject running line (#541) -- both read from ONE shared fetch.
# --------------------------------------------------------------------------- #
async def _fetch_game_logs_for_games(
    db: AsyncSession, game_ids: Sequence[int]
) -> dict[int, list[SummerLeaguePlayerGameLog]]:
    """Every resolved box-score line for ``game_ids``, grouped by game -- ONE query.

    Shared by the Live Desk's top-performer board (:func:`_top_performers_from_logs`)
    and the Live hero's two-subject running-line render
    (:func:`_hero_line_from_logs`) so both read the identical tonight's-logs
    fetch instead of issuing it twice -- the net per-request query cost stays
    flat versus the pre-#541 ``_top_performers``-only fetch (module docstring:
    "no request-time query count proportional to players or games").
    """
    if not game_ids:
        return {}
    stmt = select(SummerLeaguePlayerGameLog).where(
        SummerLeaguePlayerGameLog.game_id.in_(game_ids),  # type: ignore[attr-defined]
        SummerLeaguePlayerGameLog.player_id.is_not(None),  # type: ignore[union-attr]
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: dict[int, list[SummerLeaguePlayerGameLog]] = defaultdict(list)
    for row in rows:
        out[row.game_id].append(row)
    return dict(out)


def _played(row: SummerLeaguePlayerGameLog) -> bool:
    """Whether a box-score line represents a player who actually appeared.

    The NBA feed lists a game's full roster; players who didn't dress or logged
    a DNP get a shell line with ``minutes_seconds IS NULL`` and every box stat
    ``NULL`` (Game Score would coalesce to 0.0). Those are not performances --
    excluding them keeps DNP veterans off the Live top-performer/hero surfaces
    and off the negative/zero Game Scores a shell line would otherwise imply.
    """
    return row.minutes_seconds is not None and row.minutes_seconds > 0


def _top_performers_from_logs(
    logs_by_game: Mapping[int, Sequence[SummerLeaguePlayerGameLog]],
) -> dict[int, tuple[int, float]]:
    """Highest live GmSc tracked (resolved) player per game, from an already-fetched log set.

    Scoped to every resolved (``player_id IS NOT NULL``) line that represents an
    actual appearance (:func:`_played`) -- a resolved player in a live box score
    is, by construction, someone this app tracks, but a DNP/pre-tip shell line
    (NULL minutes) is not a performance and must never win the top-performer
    slot with a 0.0 Game Score. Pure (no I/O) since
    :func:`_fetch_game_logs_for_games` already did the fetch.
    """
    best: dict[int, tuple[int, float]] = {}
    for game_id, rows in logs_by_game.items():
        for row in rows:
            if row.player_id is None or not _played(row):
                continue
            gmsc = round(game_score_from_row(row), 2)
            current = best.get(game_id)
            if current is None or gmsc > current[1]:
                best[game_id] = (row.player_id, gmsc)
    return best


def _build_live_board(
    *,
    slate_rows: Sequence[SummerLeagueDeskSlate],
    games: dict[int, SummerLeagueGame],
    teams: dict[int, SummerLeagueTeamEntry],
    logs_by_game: Mapping[int, Sequence[SummerLeaguePlayerGameLog]],
) -> list[DeskLiveBoardRow]:
    """The Live Desk's all-games board -- every game, including the hero's."""
    top_by_game = _top_performers_from_logs(logs_by_game)
    out: list[DeskLiveBoardRow] = []
    for row in slate_rows:
        game = games.get(row.game_id)
        top = top_by_game.get(row.game_id)
        out.append(
            DeskLiveBoardRow(
                game_id=row.game_id,
                matchup_label=_matchup_label(game, teams),
                status=game.status.value if game else "unknown",
                home_score=game.home_score if game else None,
                away_score=game.away_score if game else None,
                top_performer_player_id=top[0] if top else None,
                top_performer_gmsc=top[1] if top else None,
                read=_extract_prose(row.facts, surface=Surface.TICK_NOTE),
                tip_datetime=game.tip_datetime if game else None,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# The Ledger (behavior spec §1, §4 "Performance of the Night")
# --------------------------------------------------------------------------- #
async def _resolve_ledger_date(
    db: AsyncSession, *, competition_ids: Sequence[int], today: date
) -> Optional[date]:
    """The most recent date with a final game -- "last night," even across an off-day.

    Off-day RECAP (behavior spec §2: "the flip never fires -> the Ledger
    persists all day") means "today" may have zero games while the Ledger still
    needs to show the last night that *did* have games -- so this looks back
    from ``today``, it does not assume the Ledger's games are dated ``today``.
    """
    if not competition_ids:
        return None
    stmt = select(func.max(SummerLeagueGame.game_date)).where(
        SummerLeagueGame.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
        SummerLeagueGame.status == SummerLeagueGameStatus.FINAL,  # type: ignore[arg-type]
        SummerLeagueGame.game_date <= today,  # type: ignore[operator,arg-type]
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _assemble_ledger(
    db: AsyncSession,
    *,
    competition_ids: Sequence[int],
    ledger_date: Optional[date],
    baseline_version: Optional[str],
) -> tuple[list[DeskLedgerRow], dict[int, PlayerMaster]]:
    """The Ledger's top-performers list: per-game GmSc + cohort percentile.

    Percentile ranks a single game's GmSc against the player's cohort's
    **game-grain** distribution (#539) -- the correct single-game baseline
    #525 introduced and the streak trigger already reads
    (`cohort_baselines.build_baselines`, ``grain="game"``); ranking a single
    game against the event-grain (season-blended, low-variance) distribution
    was the same approximation #525 fixed for the streak trigger, carried
    over here. A player whose cohort has no active **game-grain** baseline
    yet is skipped entirely rather than assigned a fabricated percentile off
    a mismatched grain.
    """
    if not competition_ids or ledger_date is None:
        return [], {}

    log_stmt = (
        select(SummerLeaguePlayerGameLog)
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayerGameLog.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
            SummerLeagueGame.game_date == ledger_date,  # type: ignore[arg-type]
            SummerLeaguePlayerGameLog.player_id.is_not(None),  # type: ignore[union-attr]
        )
    )
    logs = (await db.execute(log_stmt)).scalars().all()
    if not logs:
        return [], {}

    player_ids = sorted({log.player_id for log in logs if log.player_id is not None})
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

    cohort_keys = {
        cohort_key_for(
            player_by_id[pid].draft_round if pid in player_by_id else None,
            player_by_id[pid].draft_pick if pid in player_by_id else None,
            grain=SummerLeagueDeskGrain.GAME,
        )
        for pid in player_ids
    }
    baselines: dict[str, SummerLeagueCohortBaseline] = {}
    if baseline_version is not None and cohort_keys:
        baseline_stmt = select(SummerLeagueCohortBaseline).where(
            SummerLeagueCohortBaseline.baseline_version == baseline_version,  # type: ignore[arg-type]
            SummerLeagueCohortBaseline.cohort_key.in_(cohort_keys),  # type: ignore[attr-defined]
            SummerLeagueCohortBaseline.grain == SummerLeagueDeskGrain.GAME,  # type: ignore[arg-type]
            SummerLeagueCohortBaseline.is_active.is_(True),  # type: ignore[attr-defined]
        )
        baseline_rows = (await db.execute(baseline_stmt)).scalars().all()
        baselines = {b.cohort_key: b for b in baseline_rows}

    facts_by_player: dict[int, Optional[list[dict[str, object]]]] = {}
    if baseline_version is not None and player_ids:
        grade_stmt = select(SummerLeagueDeskPlayerGrade).where(
            SummerLeagueDeskPlayerGrade.player_id.in_(player_ids),  # type: ignore[attr-defined]
            SummerLeagueDeskPlayerGrade.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
            SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
        )
        for grade_row in (await db.execute(grade_stmt)).scalars().all():
            facts_by_player.setdefault(grade_row.player_id, grade_row.facts)

    rows: list[DeskLedgerRow] = []
    for log in logs:
        pid = log.player_id
        if pid is None:
            continue
        player = player_by_id.get(pid)
        cohort_key = cohort_key_for(
            player.draft_round if player else None,
            player.draft_pick if player else None,
            grain=SummerLeagueDeskGrain.GAME,
        )
        baseline = baselines.get(cohort_key)
        if baseline is None:
            continue
        gmsc = round(game_score_from_row(log), 2)
        pctl = percentile_of_value(baseline.breakpoints, gmsc)
        grade_bucket = grade_for_percentile(pctl)
        rows.append(
            DeskLedgerRow(
                game_id=log.game_id,
                player_id=pid,
                gmsc=gmsc,
                pctl=pctl,
                grade=grade_bucket.value,
                read=_extract_prose(
                    facts_by_player.get(pid), surface=Surface.LEDGER_ECHO
                ),
                pts=log.pts,
                reb=log.reb,
                ast=log.ast,
            )
        )

    rows.sort(key=lambda r: (-r.pctl, -r.gmsc))
    return rows[:MAX_LEDGER_ROWS], player_by_id


def _ledger_hero(
    ledger_rows: Sequence[DeskLedgerRow], player_by_id: dict[int, PlayerMaster]
) -> Optional[DeskHero]:
    """Performance of the Night -- the top Ledger row (behavior spec §4)."""
    if not ledger_rows:
        return None
    top = ledger_rows[0]
    player = player_by_id.get(top.player_id)
    label = (player.display_name if player else None) or f"Player {top.player_id}"
    headline = top.read or (
        f"{label} led the night at {top.gmsc:g} GmSc "
        f"({int(round(top.pctl))}th percentile)."
    )
    return DeskHero(
        kind="performance_of_night",
        game_id=top.game_id,
        subject_player_id=top.player_id,
        subject_player_id_2=None,
        headline=headline,
        tagline=None,
        facts=[],
    )


# --------------------------------------------------------------------------- #
# Class Tracker (behavior spec §7) -- stat-view rescaling (#511)
#
# Reuses the SL Explorer's rate-service roll-up primitives
# (`app.services.summer_league_explorer_service.rollup_recombinable` /
# `rollup_rate_composite`) rather than re-deriving per-mode arithmetic --
# see behavior spec §7's column taxonomy table:
#   Box family (Box / Per-36 / Per-100): PTS/REB/AST/STL/BLK/TOV rescale by
#     mode; FG%/3P%/FT% are recombined from pooled makes/attempts and are
#     therefore rate-invariant across all three modes.
#   Advanced: its own ten-column set (TS%/eFG%/USG%/AST%/TOV%/REB%/3PAr/FTr/
#     WS82/BPM), constant regardless of the Box/Per-36/Per-100 toggle.
# --------------------------------------------------------------------------- #

# Box-family counting stats that rescale per Box/Per-36/Per-100 mode.
_BOX_COUNTING_KEYS: tuple[str, ...] = ("pts", "reb", "ast", "stl", "blk", "tov")
# Box-family shooting percentages: recombined from pooled box components, so
# they render identically in all three Box/Per-36/Per-100 modes (never scaled).
_BOX_PCT_KEYS: tuple[str, ...] = ("fg_pct", "fg3_pct", "ft_pct")
# Advanced set: box-derived (recombinable, mode-independent by nature; keys
# ts_pct/efg_pct/fg3ar/ftr, handled inline below) vs. league-relative
# composites (minute-weighted; `None` on a pool that isn't `adv_eligible` --
# see `app.services.summer_league.metrics`, which stores these `None`
# outright on ineligible pools).  `rollup_rate_composite` skips `None`
# inputs, so BPM/WS82 naturally render `None` (em-dash) when every pooled
# competition row for a player is ineligible -- no extra gating needed here.
_ADV_RATE_COMPOSITE_KEYS: tuple[str, ...] = (
    "usg_pct",
    "ast_pct",
    "tov_pct",
    "trb_pct",
    "ws82",
    "bpm",
)


def _r1(value: Optional[float]) -> Optional[float]:
    """Round to 1 decimal, passing ``None`` through."""
    return round(value, 1) if value is not None else None


def _r3(value: Optional[float]) -> Optional[float]:
    """Round to 3 decimals (0-1 fraction columns: ``fg3ar``/``ftr``)."""
    return round(value, 3) if value is not None else None


def _pooled_possessions(rows: Sequence[SummerLeaguePlayerSeason]) -> Optional[float]:
    """Pace-covered possessions extrapolated across a player's pooled venue rows.

    Mirrors the denominator `summer_league_explorer_service.rollup_recombinable`
    uses for its ``"pts_per100"`` key (career-grain per-100 extrapolation over
    the 2017 pace-coverage gap, generalized here to any counting stat rather
    than only points): sum pace x minutes over pace-covered rows, then
    extrapolate to the player's *total* pooled minutes via the minute-weighted
    observed pace, so a player with partial pace coverage isn't divided by
    only the covered slice. ``None`` when no pooled row carries pace data.

    Args:
        rows: A player's pooled ``SummerLeaguePlayerSeason`` rows for one event.

    Returns:
        Extrapolated possessions, or ``None`` when no row has pace data.
    """
    covered_minutes = sum(float(r.minutes or 0) for r in rows if r.pace is not None)
    if not covered_minutes:
        return None
    total_minutes = sum(float(r.minutes or 0) for r in rows)
    pace_weighted = sum(float(r.pace or 0) * float(r.minutes or 0) for r in rows)
    return (pace_weighted / MINUTES_PER_GAME) * (total_minutes / covered_minutes)


def _build_stat_columns(
    rows: Sequence[SummerLeaguePlayerSeason], stat_view: str
) -> dict[str, Optional[float]]:
    """The Box/Per-36/Per-100/Advanced middle block for one Class Tracker row.

    ``rows`` is one player's pooled ``SummerLeaguePlayerSeason`` rows across
    every venue in the event cluster for the event year (the same pool
    `_assemble_tracker`'s fixed-frame GP/MIN/GmSc blend uses). A player with no
    rows (e.g. rostered but hasn't debuted yet -- GP=0) returns every column
    ``None`` -- the template renders that as an em-dash, matching behavior
    spec §7's "GP=0 rostered players appear with em-dashes across stat and
    rate columns."

    Args:
        rows: A player's pooled per-competition season rows for the event.
        stat_view: One of `TRACKER_STAT_VIEWS` (already validated/normalized
            by the caller).

    Returns:
        A flat ``{column_key: value}`` dict -- the box-family nine-column set
        for ``"box"``/``"per36"``/``"per100"``, or the advanced ten-column set
        for ``"advanced"``.
    """
    if stat_view == "advanced":
        return {
            "ts_pct": _r1(rollup_recombinable(rows, "ts_pct")),
            "efg_pct": _r1(rollup_recombinable(rows, "efg_pct")),
            "fg3ar": _r3(rollup_recombinable(rows, "fg3ar")),
            "ftr": _r3(rollup_recombinable(rows, "ftr")),
            **{
                key: _r1(rollup_rate_composite(rows, key))
                for key in _ADV_RATE_COMPOSITE_KEYS
            },
        }

    gp_total = sum(r.gp for r in rows)
    minutes_total = sum(float(r.minutes or 0) for r in rows)

    factor: Optional[float]
    if stat_view == "per36":
        factor = 36.0 / minutes_total if minutes_total else None
    elif stat_view == "per100":
        poss = _pooled_possessions(rows)
        factor = 100.0 / poss if poss else None
    else:  # "box" -- per-game average, the tracker's baseline display.
        factor = 1.0 / gp_total if gp_total else None

    def _scaled(key: str) -> Optional[float]:
        if factor is None:
            return None
        total = sum(float(getattr(r, key, 0) or 0) for r in rows)
        return round(total * factor, 1)

    return {
        **{key: _scaled(key) for key in _BOX_COUNTING_KEYS},
        **{key: _r1(rollup_recombinable(rows, key)) for key in _BOX_PCT_KEYS},
    }


def _tracker_cohort_predicate(
    cohort: str,
) -> Callable[[PlayerMaster, int], bool]:
    """The six Class Tracker cohort filters (behavior spec §7), over one roster pool.

    These are FILTERS over the same event-cluster roster, not a mutually
    exclusive per-player label -- e.g. a lottery pick matches both "lottery"
    and "round1" and "full_class".
    """
    predicates: dict[str, Callable[[PlayerMaster, int], bool]] = {
        "lottery": lambda p, year: (
            p.draft_year == year
            and p.draft_round == 1
            and p.draft_pick is not None
            and p.draft_pick <= 14
        ),
        "round1": lambda p, year: p.draft_year == year and p.draft_round == 1,
        "round2": lambda p, year: p.draft_year == year and p.draft_round == 2,
        "full_class": lambda p, year: p.draft_year == year and p.draft_round in (1, 2),
        # "Prior-year draftees who returned" -- exactly one class behind, per
        # #543 (previously any earlier `draft_year` was admitted, which pulled
        # in players drafted two-plus years ago).
        "sophomores": lambda p, year: p.draft_year is not None
        and p.draft_year == year - 1,
        # "No draft pick in the current class." Contract type isn't in the data
        # model (behavior spec §7) -- drafted-vs-undrafted only.
        "undrafted": lambda p, _year: p.draft_round is None,
    }
    return predicates[cohort]


async def _assemble_tracker(
    db: AsyncSession,
    *,
    competition_ids: Sequence[int],
    event_year: int,
    baseline_version: Optional[str],
    cohort: str,
    stat_view: str,
) -> tuple[DeskTrackerSection, dict[int, dict[str, Optional[str]]]]:
    """The pinned Class Tracker: fixed frame + the active stat view's middle block (#511).

    Populates every column behavior spec §7 calls "fixed" (Player/GP/MIN/GmSc/
    grade -- constant across all four stat views) plus ``stat_columns``, the
    Box/Per-36/Per-100/Advanced middle block, via `_build_stat_columns`.

    ``grade`` is READ from the persisted T2 contract
    (``summer_league_desk_player_grades``, written offline by Job B's
    ``desk_grades.grade_player_event``), never recomputed from T1 breakpoints
    at request time (#543) -- a gated T2 row (thin sample; see
    ``desk_grades.is_gated``) renders ``grade=None``, the same unqualified
    (em-dash) treatment as a never-graded player, rather than a confident-
    looking Hot/Warm/Cold label the gate ladder was designed to suppress.

    Returns the section alongside a ``{player_id: {"abbrev", "logo_url"}}``
    lookup for the rows actually returned (post-cap). `DeskTrackerRow` (#506)
    is a frozen read-model contract with no room for display assets, so this
    reuses the team-entry rows this function already fetches for
    ``identity_label`` instead of `get_desk_view_context` re-querying
    `SummerLeagueParticipation`/`SummerLeagueTeamEntry` a second time --
    zero net-new queries for row headshots/team logos.
    """
    cohort = cohort if cohort in TRACKER_COHORTS else DEFAULT_TRACKER_COHORT
    stat_view = (
        stat_view if stat_view in TRACKER_STAT_VIEWS else DEFAULT_TRACKER_STAT_VIEW
    )
    empty = DeskTrackerSection(
        cohort=cohort, stat_view=stat_view, rows=[], truncated=False
    )
    if not competition_ids:
        return empty, {}

    roster_stmt = select(  # type: ignore[call-overload]
        SummerLeagueParticipation.player_id,
        SummerLeagueParticipation.team_entry_id,
        SummerLeagueParticipation.roster_status,
    ).where(
        SummerLeagueParticipation.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
        SummerLeagueParticipation.player_id.is_not(None),  # type: ignore[union-attr]
    )
    roster_rows = (await db.execute(roster_stmt)).all()

    team_entry_by_player: dict[int, int] = {}
    player_ids: set[int] = set()
    for player_id, team_entry_id, roster_status in roster_rows:
        if player_id is None or roster_status not in _ROSTER_ACTIVE_STATUSES:
            continue
        player_ids.add(player_id)
        team_entry_by_player[player_id] = team_entry_id
    if not player_ids:
        return empty, {}

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

    predicate = _tracker_cohort_predicate(cohort)
    member_ids = [
        pid
        for pid in player_ids
        if (player := player_by_id.get(pid)) is not None
        and predicate(player, event_year)
    ]
    if not member_ids:
        return empty, {}

    # Full rows (not just the gp/minutes/gmsc slice) so `_build_stat_columns`
    # has box totals + advanced composites to pool per player, across every
    # venue in the event cluster this year.
    season_stmt = select(SummerLeaguePlayerSeason).where(
        SummerLeaguePlayerSeason.player_id.in_(member_ids),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.year == event_year,  # type: ignore[arg-type]
    )
    season_rows = (await db.execute(season_stmt)).scalars().all()
    events = blend_event_aggregates(season_rows, min_minutes=0.0)
    season_rows_by_player: dict[int, list[SummerLeaguePlayerSeason]] = defaultdict(list)
    for row in season_rows:
        season_rows_by_player[row.player_id].append(row)

    teams = await _fetch_team_entries(
        db, [team_entry_by_player.get(pid) for pid in member_ids]
    )

    # Read the persisted T2 grade contract (#543) -- never recompute a
    # percentile/grade at request time. Job B's tick (`grade_player_event`)
    # already ranked each member's event-aggregate GmSc against T1 and applied
    # the adaptive gate ladder; this batch-reads that outcome instead of
    # re-deriving it from T1 breakpoints directly (which -- unlike the tick --
    # would render a confident-looking grade for a thin/one-game sample the
    # gate ladder was designed to suppress). A player can carry a graded row
    # per competition in the cluster (Job B grades once per active roster per
    # competition); `setdefault` keeps the first (ordered) row since the
    # underlying event-aggregate `subject_value` is identical across a
    # player's rows for the same `baseline_version`.
    grade_by_player: dict[int, SummerLeagueDeskPlayerGrade] = {}
    if baseline_version is not None and member_ids:
        grade_stmt = (
            select(SummerLeagueDeskPlayerGrade)
            .where(
                SummerLeagueDeskPlayerGrade.player_id.in_(member_ids),  # type: ignore[attr-defined]
                SummerLeagueDeskPlayerGrade.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
                SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
            )
            .order_by(SummerLeagueDeskPlayerGrade.competition_id.asc())  # type: ignore[attr-defined]
        )
        for grade_row in (await db.execute(grade_stmt)).scalars().all():
            grade_by_player.setdefault(grade_row.player_id, grade_row)

    rows: list[DeskTrackerRow] = []
    for pid in member_ids:
        player = player_by_id[pid]
        agg = events.get((pid, event_year))
        gp = agg.gp if agg else 0
        minutes = round(agg.minutes, 1) if agg else 0.0
        gmsc = agg.gmsc if agg else None

        # A gated grade renders as unqualified -- no Hot/Warm/Cold label --
        # same "degrade, never fabricate" contract #539 established for the
        # Ledger's per-game percentile. `grade=None` already renders the
        # template's em-dash (see `class_tracker.html`), so gated and
        # never-graded (no tick has run for this player/version yet) collapse
        # to the same rendering with no separate branch needed.
        grade: Optional[str] = None
        member_grade = grade_by_player.get(pid)
        if member_grade is not None and not member_grade.gated:
            grade = member_grade.grade.value

        abbrev = _team_label(teams.get(team_entry_by_player.get(pid, -1)))
        position = player.position or "-"
        if player.draft_round is None:
            identity_label = f"Undrafted · {abbrev}"
        else:
            overall = draft_slot_fallback(player.draft_round, player.draft_pick)
            identity_label = (
                f"#{overall} · {abbrev} · {position}"
                if overall
                else f"{abbrev} · {position}"
            )

        rows.append(
            DeskTrackerRow(
                player_id=pid,
                display_name=player.display_name or f"Player {pid}",
                identity_label=identity_label,
                gp=gp,
                minutes=minutes,
                gmsc=gmsc,
                grade=grade,
                stat_columns=_build_stat_columns(
                    season_rows_by_player.get(pid, []), stat_view
                ),
            )
        )

    rows.sort(key=lambda r: (r.gmsc is None, -(r.gmsc or 0.0)))
    truncated = len(rows) > TRACKER_CAP
    capped_rows = rows[:TRACKER_CAP]

    tracker_teams: dict[int, dict[str, Optional[str]]] = {}
    for tracker_row in capped_rows:
        team_entry = teams.get(team_entry_by_player.get(tracker_row.player_id, -1))
        if team_entry is not None:
            tracker_teams[tracker_row.player_id] = {
                "abbrev": _team_label(team_entry),
                "logo_url": franchise_logo_url(team_entry.nba_stats_team_id),
            }

    section = DeskTrackerSection(
        cohort=cohort, stat_view=stat_view, rows=capped_rows, truncated=truncated
    )
    return section, tracker_teams


# --------------------------------------------------------------------------- #
# View-context enrichment (#509; relocated from `app.routes.ui` per follow-up
# ticket -- routes must stay thin, and #508's contract is "one service call
# assembles the page payload")
# --------------------------------------------------------------------------- #
async def get_desk_view_context(
    db: AsyncSession, payload: Optional[DeskPayload]
) -> dict[str, dict]:
    """Batch-enrich a Desk payload with the player identity + team-logo data templates need.

    `DeskPayload` (#506) deliberately carries only ids -- `subject_player_id`,
    `top_performer_player_id`, ledger `player_id`, `game_id` -- no display
    names, headshots, or team crests (see that module's docstring: it's an
    internal read-model, not a view model). Templates need those, so this
    does ONE small, batched enrichment pass per render rather than resolving
    per-row inside a template (which would silently reintroduce an N+1 into
    `/`'s query budget). Fires at most two round trips total (players, then
    games+teams) regardless of how many rows/games the payload has.

    Kept as a companion structure -- a plain dict keyed by id -- rather than
    folded into `DeskPayload` itself: that dataclass (and its nested
    hero/slate/live_board/ledger/tracker rows) is a frozen contract #511 and
    #522 also build against.

    Args:
        db: Active database session.
        payload: The current render's Desk payload, or `None` off-window (in
            which case this returns empty lookups without querying anything).

    Returns:
        `{"players": {player_id: {...}}, "matchups": {game_id: {...}}}`.
        Each player entry has `display_name`, `slug`, `photo_url`,
        `draft_tag` (e.g. "Pick 5", "Undrafted"). Each matchup entry has
        `home`/`away`, each `{"abbrev": str, "logo_url": Optional[str]}`.
    """
    if payload is None:
        return {"players": {}, "matchups": {}}

    player_ids: set[int] = set()
    if payload.hero.subject_player_id is not None:
        player_ids.add(payload.hero.subject_player_id)
    if payload.hero.subject_player_id_2 is not None:
        player_ids.add(payload.hero.subject_player_id_2)
    for live_row in payload.live_board:
        if live_row.top_performer_player_id is not None:
            player_ids.add(live_row.top_performer_player_id)
    for ledger_row in payload.ledger:
        player_ids.add(ledger_row.player_id)
    # Class Tracker rows (#511) -- folded into the SAME batched PlayerMaster
    # query below, so headshots/slugs cost nothing extra over #509's budget.
    for tracker_row in payload.tracker.rows:
        player_ids.add(tracker_row.player_id)

    game_ids: set[int] = set()
    if payload.hero.game_id is not None:
        game_ids.add(payload.hero.game_id)
    for slate_row in payload.slate:
        game_ids.add(slate_row.game_id)
    for live_row in payload.live_board:
        game_ids.add(live_row.game_id)
    for ledger_row in payload.ledger:
        game_ids.add(ledger_row.game_id)

    players: dict[int, dict] = {}
    if player_ids:
        player_rows = (
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
        for p in player_rows:
            if p.id is None:
                continue
            if p.draft_round is None:
                draft_tag = "Undrafted"
            else:
                overall = draft_slot_fallback(p.draft_round, p.draft_pick)
                draft_tag = f"Pick {overall}" if overall else "Drafted"
            players[p.id] = {
                "display_name": p.display_name or f"Player {p.id}",
                "slug": p.slug,
                "photo_url": (
                    get_player_image_url(player_id=p.id, slug=p.slug, style="default")
                    if p.slug
                    else get_placeholder_url(
                        p.display_name, player_id=p.id, width=160, height=160
                    )
                ),
                "position": p.position,
                "draft_tag": draft_tag,
            }

    matchups: dict[int, dict] = {}
    if game_ids:
        game_rows = (
            (
                await db.execute(
                    select(SummerLeagueGame).where(  # type: ignore[call-overload]
                        SummerLeagueGame.id.in_(game_ids)  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
        )
        team_ids: set[int] = set()
        for g in game_rows:
            if g.home_team_entry_id is not None:
                team_ids.add(g.home_team_entry_id)
            if g.away_team_entry_id is not None:
                team_ids.add(g.away_team_entry_id)

        teams: dict[int, SummerLeagueTeamEntry] = {}
        if team_ids:
            team_rows = (
                (
                    await db.execute(
                        select(SummerLeagueTeamEntry).where(  # type: ignore[call-overload]
                            SummerLeagueTeamEntry.id.in_(team_ids)  # type: ignore[union-attr]
                        )
                    )
                )
                .scalars()
                .all()
            )
            teams = {t.id: t for t in team_rows if t.id is not None}

        def _side(team_entry_id: Optional[int]) -> dict:
            team = teams.get(team_entry_id) if team_entry_id is not None else None
            if team is None:
                return {"abbrev": "TBD", "logo_url": None}
            return {
                "abbrev": team.raw_team_abbreviation or team.raw_team_name or "TBD",
                "logo_url": franchise_logo_url(team.nba_stats_team_id),
            }

        for g in game_rows:
            if g.id is None:
                continue
            matchups[g.id] = {
                "home": _side(g.home_team_entry_id),
                "away": _side(g.away_team_entry_id),
            }

    return {"players": players, "matchups": matchups}


@dataclass(frozen=True)
class DeskView:
    """The full `/` route's Desk data: the read-model payload plus its view-context.

    `payload` is `None` off-window (the route renders the collapsed archive
    strip in that case); `players`/`matchups` are always present but empty
    when there is nothing to enrich (off-window, or an in-window payload that
    references no players/games -- doesn't happen in practice, but the
    enrichment pass degrades to empty dicts rather than raising). `tracker_teams`
    (#511) is a `{player_id: {"abbrev", "logo_url"}}` lookup for Class Tracker
    rows, sourced from `_assemble_tracker`'s own team-entry fetch (see that
    function's docstring for why it isn't folded into `players`/`matchups`).
    """

    payload: Optional[DeskPayload]
    players: dict[int, dict]
    matchups: dict[int, dict]
    tracker_teams: dict[int, dict[str, Optional[str]]]


async def get_desk_view(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    tracker_cohort: str = DEFAULT_TRACKER_COHORT,
    tracker_stat_view: str = DEFAULT_TRACKER_STAT_VIEW,
) -> DeskView:
    """The ONE call `app.routes.ui.home` makes to assemble the Desk for `/`.

    Composes `_assemble_desk_payload` (the pure read-model, shared with
    `get_desk_payload`) with `get_desk_view_context` (the player/team
    enrichment templates need) so the route itself never touches
    `select()`/`db.execute` for the Desk -- it just unpacks this one result
    into the template context. See `get_desk_view_context`'s docstring for why
    the enrichment stays a companion structure instead of a payload field.

    Args:
        db: Active database session (read-only).
        now: Override for "now" (tests; defaults to the current UTC instant).
        tracker_cohort: Forwarded to the payload assembly.
        tracker_stat_view: Forwarded to the payload assembly.

    Returns:
        A `DeskView` with the resolved payload (`None` off-window) and its
        player/matchup/tracker-team enrichment dicts (empty off-window).
    """
    payload, tracker_teams = await _assemble_desk_payload(
        db,
        now=now,
        tracker_cohort=tracker_cohort,
        tracker_stat_view=tracker_stat_view,
    )
    view_context = await get_desk_view_context(db, payload)
    return DeskView(
        payload=payload,
        players=view_context["players"],
        matchups=view_context["matchups"],
        tracker_teams=tracker_teams,
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
async def get_desk_payload(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    tracker_cohort: str = DEFAULT_TRACKER_COHORT,
    tracker_stat_view: str = DEFAULT_TRACKER_STAT_VIEW,
) -> Optional[DeskPayload]:
    """Assemble one `/` render's Summer League Desk payload, or ``None`` off-window.

    One call, no per-player/per-game queries (see module docstring). Called by
    `get_desk_view` (which also runs the view-context enrichment); the `/`
    route is responsible for the off-window UI treatment when this returns
    ``None`` -- this function's only job is to return the correct answer, not
    to render anything.

    Args:
        db: Active database session (read-only; this function never writes).
        now: Override for "now" (tests; defaults to the current UTC instant).
        tracker_cohort: One of `TRACKER_COHORTS`; falls back to
            `DEFAULT_TRACKER_COHORT` when unset/unrecognized (e.g. a bad query
            param). Wired end-to-end by #511's toggle UI.
        tracker_stat_view: One of `TRACKER_STAT_VIEWS`; same fallback behavior.

    Returns:
        The fully-resolved :class:`~app.services.event_desk.payload.DeskPayload`
        for the current state, or ``None`` when the SL event's lifecycle isn't
        currently ``active`` (off-window).
    """
    payload, _tracker_teams = await _assemble_desk_payload(
        db,
        now=now,
        tracker_cohort=tracker_cohort,
        tracker_stat_view=tracker_stat_view,
    )
    return payload


def _effective_now(now: Optional[datetime]) -> datetime:
    """Resolve the request instant, honoring `settings.sl_desk_force_date`.

    `settings.sl_desk_force_date` is the framework doc's "config ... date
    override" lever (`docs/plans/event-desk-framework.md`: "Window source =
    schedule-driven with a config force-on/off & date override") -- an
    operator-set calendar date that every Desk request/tick resolves "today"
    against instead of the real wall-clock date, for demoing/QAing a specific
    day without waiting for the actual calendar. The time-of-day component of
    ``now`` (explicit or real wall-clock) is preserved so live-vs-scheduled
    comparisons still behave realistically on the overridden date.

    Args:
        now: Caller-supplied override (tests), or ``None`` to use the current
            UTC instant.

    Returns:
        The naive-UTC instant to resolve the Desk against.
    """
    resolved = (
        now if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    )
    if settings.sl_desk_force_date is not None:
        resolved = datetime.combine(settings.sl_desk_force_date, resolved.time())
    return resolved


async def _assemble_desk_payload_body(
    db: AsyncSession,
    *,
    window: _WindowState,
    today: date,
    baseline_version: Optional[str],
    freshness: DeskFreshness,
    daily_state: EventDailyState,
    tracker_cohort: str,
    tracker_stat_view: str,
) -> tuple[DeskPayload, dict[int, dict[str, Optional[str]]]]:
    """Assemble hero/slate/live_board/ledger/tracker for ONE (state, cohort, stat_view).

    Split out of the old `_assemble_desk_payload` so the window/baseline/today/
    freshness resolution (five-ish queries) happens exactly ONCE per tick or
    request, while this body -- the part that actually varies per variant --
    can be called repeatedly. Two callers:

    * `_assemble_desk_payload` (single-request path behind `get_desk_payload`/
      `get_desk_view`): resolves `window` once, then calls this with
      ``daily_state=window.daily_state`` -- i.e. exactly today's pre-refactor
      behavior, byte-for-byte.
    * `build_desk_render_variants` (tick-time materialization, launch-readiness
      item 10): resolves `window`/`baseline_version`/`today`/`freshness` ONCE
      per tick, then calls this once per `(daily_state, tracker_cohort,
      tracker_stat_view)` in the full variant matrix -- `daily_state` here is
      NOT necessarily `window.daily_state` (the tick builds all three states
      unconditionally so a tip-time transition is already materialized before
      it happens; see that function's docstring).

    Args:
        db: Active database session.
        window: The already-resolved `_WindowState` (event_row/is_home_owner/
            competition_ids) -- `daily_state` below is what actually selects
            the Ledger vs. Preview/Live branch, not `window.daily_state`.
        today: The resolved Eastern "today" date.
        baseline_version: The active T1 baseline version, or `None`.
        freshness: The pre-built `DeskFreshness` stamp for this payload.
        daily_state: Which of the three states to build this call's hero/
            slate/live_board/ledger for.
        tracker_cohort: One of `TRACKER_COHORTS` (validated by `_assemble_tracker`).
        tracker_stat_view: One of `TRACKER_STAT_VIEWS` (validated by `_assemble_tracker`).

    Returns:
        The assembled `DeskPayload` for this exact combination, plus its
        `tracker_teams` companion lookup.
    """
    hero: Optional[DeskHero] = None
    slate: list[DeskSlateRow] = []
    live_board: list[DeskLiveBoardRow] = []
    ledger: list[DeskLedgerRow] = []

    if daily_state == EventDailyState.RECAP:
        ledger_date = await _resolve_ledger_date(
            db, competition_ids=window.competition_ids, today=today
        )
        ledger, player_by_id = await _assemble_ledger(
            db,
            competition_ids=window.competition_ids,
            ledger_date=ledger_date,
            baseline_version=baseline_version,
        )
        hero = _ledger_hero(ledger, player_by_id)
    else:
        slate_rows = await _fetch_today_slate(
            db, competition_ids=window.competition_ids, today=today
        )
        # Zero-signal days still retain their schedule (#544): the slate/live
        # board list every game today regardless of storyline weight -- only
        # the HERO's framing changes when nothing clears a storyline
        # threshold (`slate_needs_quiet_fallback`), falling through to the
        # quiet-slate class-leader hero below instead of a game-based one.
        quiet = slate_needs_quiet_fallback(slate_rows)  # type: ignore[arg-type]
        if slate_rows:
            game_ids = [row.game_id for row in slate_rows]
            games = await _fetch_games(db, game_ids)
            team_ids: list[Optional[int]] = []
            for game in games.values():
                team_ids.extend([game.home_team_entry_id, game.away_team_entry_id])
            teams = await _fetch_team_entries(db, team_ids)

            live = daily_state == EventDailyState.LIVE
            # #541 -- fetched ONCE (only when live; Morning has nothing to
            # show yet), then shared by both the hero's two-subject running
            # line and the Live board's top-performer column below, so this
            # doesn't add a second query on top of the pre-#541 fetch.
            logs_by_game: dict[int, list[SummerLeaguePlayerGameLog]] = {}
            if live:
                logs_by_game = await _fetch_game_logs_for_games(db, game_ids)

            hero_game_id: Optional[int] = None
            if not quiet:
                hero_row = _pick_hero_slate_row(slate_rows, games, live=live)
                assert hero_row is not None  # slate_rows is non-empty here
                hero_game_id = hero_row.game_id
                hero = await _build_game_hero(
                    db,
                    hero_row,
                    games,
                    teams,
                    kind="live_duel" if live else "marquee",
                    logs_by_game=logs_by_game,
                )

            # A quiet slate excludes nothing (no game earns the hero slot);
            # a signal-bearing slate still excludes the hero's own game, same
            # as before -- "1-game day -> empty slate, hero carries it"
            # (behavior spec §5) only applies once a game IS the hero.
            slate = _build_slate_rows(
                slate_rows, games, teams, exclude_game_id=hero_game_id
            )
            if live:
                live_board = _build_live_board(
                    slate_rows=slate_rows,
                    games=games,
                    teams=teams,
                    logs_by_game=logs_by_game,
                )
        # else: no games today at all -- slate/live_board stay empty; hero
        # falls through to the quiet-slate class-leader fallback below.

    if hero is None:
        hero = await _quiet_slate_hero(
            db,
            competition_ids=window.competition_ids,
            baseline_version=baseline_version,
        )
    if hero is None:
        hero = _FALLBACK_HERO

    tracker, tracker_teams = await _assemble_tracker(
        db,
        competition_ids=window.competition_ids,
        event_year=today.year,
        baseline_version=baseline_version,
        cohort=tracker_cohort,
        stat_view=tracker_stat_view,
    )

    payload = DeskPayload(
        daily_state=daily_state.value,
        is_home_owner=window.is_home_owner,
        hero=hero,
        slate=slate,
        live_board=live_board,
        ledger=ledger,
        tracker=tracker,
        freshness=freshness,
    )
    return payload, tracker_teams


async def _assemble_desk_payload(
    db: AsyncSession,
    *,
    now: Optional[datetime],
    tracker_cohort: str,
    tracker_stat_view: str,
) -> tuple[Optional[DeskPayload], dict[int, dict[str, Optional[str]]]]:
    """Shared body behind `get_desk_payload`/`get_desk_view` (#511).

    Split out so `get_desk_view` can reuse `_assemble_tracker`'s own
    team-entry fetch for row logos (see `DeskView.tracker_teams`) without
    `get_desk_payload`'s public, frozen-shape contract (`Optional[DeskPayload]`
    only) having to change.

    Honors `settings.sl_desk_force_mode` (#544): ``"off"`` short-circuits to
    off-window unconditionally (a kill switch, ahead of even the cheap
    `events` lookup); ``"on"`` bypasses `_resolve_window_state`'s
    `is_home_owner` gate while still requiring an actual Active/Wind-down
    lifecycle window. ``"auto"`` (default) requires both -- see
    `_resolve_window_state`'s docstring.
    """
    resolved_now = _effective_now(now)

    if settings.sl_desk_force_mode == "off":
        return None, {}

    window = await _resolve_window_state(
        db, now=resolved_now, require_owner=settings.sl_desk_force_mode != "on"
    )
    if window is None:
        return None, {}

    baseline_version = await _active_baseline_version(db)
    today = to_eastern_date(resolved_now)
    freshness = await _freshness_for(db, window.event_row, now=resolved_now)

    return await _assemble_desk_payload_body(
        db,
        window=window,
        today=today,
        baseline_version=baseline_version,
        freshness=freshness,
        daily_state=window.daily_state,
        tracker_cohort=tracker_cohort,
        tracker_stat_view=tracker_stat_view,
    )


# --------------------------------------------------------------------------- #
# Tick-time variant materialization (launch-readiness item 10 / #551)
# --------------------------------------------------------------------------- #

# Every `EventDailyState` a materialized variant matrix covers. Unlike a live
# request (which only ever resolves ONE current state via `_resolve_window_state`),
# a tick builds all three unconditionally so a tip-time Preview->Live (or
# Live->Recap) transition is already sitting in the table before the transition
# happens -- see `build_desk_render_variants`.
DESK_RENDER_DAILY_STATES: tuple[EventDailyState, ...] = (
    EventDailyState.PREVIEW,
    EventDailyState.LIVE,
    EventDailyState.RECAP,
)


@dataclass(frozen=True)
class DeskRenderVariant:
    """One `(daily_state, tracker_cohort, tracker_stat_view)` slice of one tick's variant matrix.

    Deliberately doesn't carry `event_id` or the freshness-stamp fields
    `RenderSnapshotWrite` (`app.services.event_desk.render_snapshots`) needs --
    this module can't import that one at module scope without a circular
    import (`render_snapshots.py` imports `DeskView` from here), so the
    conversion to `RenderSnapshotWrite` happens in the caller
    (`scripts/sl_desk_tick.py`), which already imports both freely.
    """

    daily_state: EventDailyState
    tracker_cohort: str
    tracker_stat_view: str
    view: DeskView


async def build_desk_render_variants(
    db: AsyncSession, *, now: Optional[datetime] = None
) -> Optional[tuple[int, list[DeskRenderVariant]]]:
    """Build the COMPLETE Preview/Live/Recap x Tracker cohort/stat-view variant matrix.

    Called as the FINAL step of the Summer League Desk's hourly tick
    (`scripts/sl_desk_tick.py::run_desk_tick`, launch-readiness item 10),
    only after every required upstream step (scoreboard/live-refresh/
    normalize/grades/storylines/commentary/`event_desk_state` freshness) has
    genuinely succeeded -- the caller wraps the whole tick in one transaction
    (`db.begin()`), so a failure anywhere upstream (or in this function
    itself) rolls back this tick's writes wholesale and leaves whatever
    snapshots the PRIOR successful tick wrote untouched.

    Reuses the exact same assembly primitives a live per-request read used to
    run inline (`_resolve_window_state`, `_active_baseline_version`,
    `_freshness_for`, `_assemble_desk_payload_body`, `_assemble_tracker`,
    `get_desk_view_context`) so every materialized variant is byte-for-byte
    what a live request would have produced for that exact
    `(daily_state, tracker_cohort, tracker_stat_view)` combination -- just
    computed once per tick instead of once per visitor. `window` /
    `baseline_version` / `today` / `freshness` are resolved ONCE here (they
    don't vary across the matrix); only the per-variant body loop below
    re-queries per combination.

    Unlike a live request (which only ever resolves ONE current `daily_state`
    via `_resolve_window_state`), this builds all three `EventDailyState`
    values unconditionally: a tip-time Preview->Live (or Live->Recap)
    transition must render correctly the INSTANT "now" crosses that boundary
    (behavior spec §2), not wait for the next hourly tick to materialize the
    new state. Building all three every tick is what makes that true --
    the request-time reader (`get_desk_view_from_snapshot`) just picks
    whichever pre-built row matches its own fresh state resolution.

    Honors `settings.sl_desk_force_mode`/`sl_desk_force_date` identically to
    `_assemble_desk_payload` (via `_effective_now` + the same force-mode
    branch), so a force-mode QA session sees the SAME variants a tick would
    have written under that override -- request-time reads and tick-time
    writes never disagree about whether the event is in-window.

    Args:
        db: Active database session (caller controls the transaction; this
            function never commits).
        now: Override for "now" (tests; defaults to the current UTC instant).

    Returns:
        `None` when the event is off-window (nothing to materialize -- the
        caller must leave any prior snapshots untouched, never truncate
        them). Otherwise `(event_id, variants)`; `variants` always has
        `len(DESK_RENDER_DAILY_STATES) * len(TRACKER_COHORTS) *
        len(TRACKER_STAT_VIEWS)` entries (3 x 6 x 4 = 72 today), one per
        unique `(daily_state, tracker_cohort, tracker_stat_view)` key.
    """
    resolved_now = _effective_now(now)

    if settings.sl_desk_force_mode == "off":
        return None

    window = await _resolve_window_state(
        db, now=resolved_now, require_owner=settings.sl_desk_force_mode != "on"
    )
    if window is None:
        return None
    assert window.event_row.id is not None
    event_id = window.event_row.id

    baseline_version = await _active_baseline_version(db)
    today = to_eastern_date(resolved_now)
    freshness = await _freshness_for(db, window.event_row, now=resolved_now)

    variants: list[DeskRenderVariant] = []
    for daily_state in DESK_RENDER_DAILY_STATES:
        for cohort in TRACKER_COHORTS:
            for stat_view in TRACKER_STAT_VIEWS:
                payload, tracker_teams = await _assemble_desk_payload_body(
                    db,
                    window=window,
                    today=today,
                    baseline_version=baseline_version,
                    freshness=freshness,
                    daily_state=daily_state,
                    tracker_cohort=cohort,
                    tracker_stat_view=stat_view,
                )
                view_context = await get_desk_view_context(db, payload)
                view = DeskView(
                    payload=payload,
                    players=view_context["players"],
                    matchups=view_context["matchups"],
                    tracker_teams=tracker_teams,
                )
                variants.append(
                    DeskRenderVariant(
                        daily_state=daily_state,
                        tracker_cohort=cohort,
                        tracker_stat_view=stat_view,
                        view=view,
                    )
                )

    return event_id, variants


# --------------------------------------------------------------------------- #
# Request-time snapshot-backed read (launch-readiness item 10 / #551)
# --------------------------------------------------------------------------- #
async def get_desk_view_from_snapshot(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    tracker_cohort: str = DEFAULT_TRACKER_COHORT,
    tracker_stat_view: str = DEFAULT_TRACKER_STAT_VIEW,
) -> DeskView:
    """The fast, snapshot-backed read `app.routes.ui.home` calls in place of `get_desk_view`.

    Resolves the CURRENT state fresh (`_resolve_window_state`, unchanged --
    the same pure resolver a live request always used, so tip-time Preview->
    Live/Live->Recap switching is exactly as correct as it always was) and
    then does exactly ONE indexed lookup
    (`app.services.event_desk.render_snapshots.get_render_snapshot`) for the
    matching `(event_id, daily_state, tracker_cohort, tracker_stat_view)`
    variant. Never queries `players_master`, `summer_league_games`,
    `summer_league_desk_player_grades`, or any other per-player/per-game
    table after that point -- the entire payload/view-context is decoded
    straight out of the snapshot row's JSON columns.

    **Never falls back to `get_desk_view`'s full assembly.** A missing
    snapshot (this exact variant hasn't been materialized yet -- e.g. the
    first tick after a fresh deploy hasn't run, or a schema-version bump
    made an old row unreadable) degrades HONESTLY to the same empty
    `DeskView` the off-window case renders (`desk_payload=None` -> the `/`
    route's collapsed archive-strip treatment) rather than reconstructing the
    page query-by-query. This is the whole point of the launch-readiness
    "render snapshot persistence" work: a cold homepage request must never be
    able to trigger the 71-query assembler again.

    Freshness is recomputed HERE against `now` (this request's instant),
    from the snapshot row's own `source_freshness_tick_at`/
    `source_freshness_next_tick_eta` columns -- never trusted verbatim off
    the persisted `DeskPayload.freshness`, which was computed relative to the
    TICK's own "now" at write time (always "fresh" relative to itself; see
    `_build_freshness`'s docstring). This is what lets a cron outage still
    honestly render "-- stale" on a page that's reading a snapshot, not a
    live query.

    Args:
        db: Active database session (read-only).
        now: Override for "now" (tests; defaults to the current UTC instant).
        tracker_cohort: One of `TRACKER_COHORTS`; falls back to
            `DEFAULT_TRACKER_COHORT` when unset/unrecognized.
        tracker_stat_view: One of `TRACKER_STAT_VIEWS`; same fallback.

    Returns:
        The matching `DeskView`, or an empty one (`payload=None`, empty
        enrichment dicts) when off-window OR the exact variant hasn't been
        materialized yet OR its `schema_version` is one this build's codec
        no longer understands.
    """
    # Deferred import: `app.services.event_desk.render_snapshots` imports
    # `DeskView` from THIS module at its own module scope, so a top-level
    # import here would be circular. Safe as a function-local import -- by
    # the time any request reaches this function, both modules have long
    # since finished importing.
    from app.services.event_desk.render_snapshots import (
        UnsupportedRenderSnapshotSchemaVersion,
        deserialize_desk_view,
        get_render_snapshot,
    )

    resolved_now = _effective_now(now)
    empty = DeskView(payload=None, players={}, matchups={}, tracker_teams={})

    if settings.sl_desk_force_mode == "off":
        return empty

    window = await _resolve_window_state(
        db, now=resolved_now, require_owner=settings.sl_desk_force_mode != "on"
    )
    if window is None:
        return empty
    assert window.event_row.id is not None

    cohort = (
        tracker_cohort if tracker_cohort in TRACKER_COHORTS else DEFAULT_TRACKER_COHORT
    )
    stat_view = (
        tracker_stat_view
        if tracker_stat_view in TRACKER_STAT_VIEWS
        else DEFAULT_TRACKER_STAT_VIEW
    )

    row = await get_render_snapshot(
        db,
        event_id=window.event_row.id,
        daily_state=window.daily_state,
        tracker_cohort=cohort,
        tracker_stat_view=stat_view,
    )
    if row is None:
        # Not yet materialized for this exact variant -- degrade honestly,
        # never fall back to `get_desk_view`'s full assembly.
        return empty

    try:
        view = deserialize_desk_view(
            payload_json=row.payload_json,
            view_context_json=row.view_context_json,
            schema_version=row.schema_version,
        )
    except UnsupportedRenderSnapshotSchemaVersion:
        # A row this build's codec can't decode -- honest degrade, same as
        # "missing," never a 500 and never the full assembler.
        return empty

    if view.payload is None:
        # Defensive: a snapshot should never be persisted with a null
        # payload for an in-window variant, but the codec stays total (see
        # `serialize_desk_view`'s docstring) -- degrade honestly rather than
        # assume.
        return empty

    fresh = _build_freshness(
        last_tick_at=row.source_freshness_tick_at,
        next_tick_eta=row.source_freshness_next_tick_eta,
        now=resolved_now,
    )
    payload = dataclass_replace(view.payload, freshness=fresh)
    return dataclass_replace(view, payload=payload)


__all__ = [
    "DEFAULT_TRACKER_COHORT",
    "DEFAULT_TRACKER_STAT_VIEW",
    "DESK_RENDER_DAILY_STATES",
    "MAX_LEDGER_ROWS",
    "TRACKER_CAP",
    "TRACKER_COHORTS",
    "TRACKER_STAT_VIEWS",
    "DeskRenderVariant",
    "DeskView",
    "build_desk_render_variants",
    "get_desk_payload",
    "get_desk_view",
    "get_desk_view_context",
    "get_desk_view_from_snapshot",
]

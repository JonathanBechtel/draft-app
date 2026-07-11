"""Job B step 4 read layer -- the peer/context populations the #520 fact detectors need.

`app.services.summer_league.desk_facts` (#520) deliberately built eight
detectors as **pure** ``(subject, context) -> Fact | None`` functions that
never touch a session -- each takes an already-fetched, plain-data input
(:class:`~app.services.summer_league.desk_facts.CohortPeer`,
:class:`~app.services.summer_league.desk_facts.GameLine`, etc.). Only
``detect_percentile`` has an input (:class:`~app.services.summer_league.desk_grades.GradeRow`)
that's a straight read of an existing T2 row -- the other seven each need a
**caller-fetched peer population** this module supplies
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §11 Stage 1; #524).

**Batched, not per-player** (ticket #524 CRITICAL constraint: "this runs
hourly ... do not introduce an N+1 over players"). Every async function here
takes a *collection* of player_ids/cohort_keys and returns a
``player_id``/``cohort_key`` -keyed dict from ONE query (or, for
:func:`fetch_cohort_members`, one query per distinct ``(season_range,
min_minutes)`` pair -- normally exactly one, since one Job A run shares both
across every cohort). Callers loop over the returned dicts in Python, never
re-querying per subject.

**Reuses, doesn't re-derive:**

* :func:`~app.services.summer_league.cohort_baselines.blend_event_aggregates`
  / :func:`~app.services.summer_league.cohort_baselines.cohort_key_for` (#502)
  -- the exact same event-aggregate blend and slot/round/status
  classification Job A used to build T1, applied here to fetch labeled
  per-member rows instead of collapsing them into breakpoints.
* :func:`~app.services.summer_league.desk_grades.percentile_of_value` (#503),
  applied here to the **game-grain** T1 baseline (#525) for per-game
  percentiles -- :func:`fetch_game_baselines` fetches those rows the same way
  :func:`fetch_event_baselines`/:func:`fetch_debut_baselines` fetch their
  grains.
* :func:`~app.services.summer_league.metrics.game_score_from_row` --
  the single source of GmSc (`game_score_line()`).

**The "count club" / "first since" threshold is query-derived, not a magic
number.** The behavior spec pins no specific numeric bar for "8-rookie club
since 2017" -- that copy is mockup flavor, not a pinned business rule. Rather
than fabricate an arbitrary constant, :data:`COUNT_CLUB_BREAKPOINT_PCTL`
reuses the cohort's own T1 baseline 90th-percentile breakpoint as the
qualifying bar: real, reproducible, and traceable to the exact same
distribution every other percentile/grade on the Desk already reads from
(spec §8: "every displayed sentence must trace to a query"). This is a
documented judgment call, not invented data.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueGame, SummerLeaguePlayerGameLog
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.cohort_baselines import (
    DEFAULT_GAME_MIN_MINUTES,
    FirstQualifyingGame,
    blend_event_aggregates,
    cohort_key_for,
    first_qualifying_games,
)
from app.services.summer_league.desk_facts import (
    ClubMember,
    CohortPeer,
    GameLine,
    PriorEvent,
    PriorHolder,
)
from app.services.summer_league.desk_grades import percentile_of_value
from app.services.summer_league.metrics import game_score_from_row

# The T1 breakpoints percentile key used as the "count club"/"first since"
# qualifying bar (see module docstring). Breakpoints are fit at 0-100 step 5
# (`cohort_baselines.DEFAULT_BREAKPOINT_PERCENTILES`), so "90" always exists
# on a non-empty breakpoints map.
COUNT_CLUB_BREAKPOINT_PCTL = "90"


def _parse_season_range(season_range: str) -> tuple[int, int]:
    """Split ``"2017-2025"`` into ``(2017, 2025)`` (mirrors ``cohort_baselines``)."""
    start_str, _sep, end_str = season_range.partition("-")
    return int(start_str), int(end_str)


# --------------------------------------------------------------------------- #
# Historical cohort population (cohort_rank, count_club, first_since)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CohortMember:
    """One historical (player, year) event-aggregate belonging to a cohort_key."""

    player_id: int
    player_label: str
    year: int
    value: float


async def fetch_cohort_members(
    db: AsyncSession, *, baselines_by_cohort: Mapping[str, SummerLeagueCohortBaseline]
) -> dict[str, list[CohortMember]]:
    """Every historical (player, year) event belonging to any of ``baselines_by_cohort``.

    Reruns Job A's own blend/classify path (`cohort_baselines.build_baselines`)
    but keeps labeled per-member rows instead of collapsing them into
    breakpoints -- this is the shared population :func:`cohort_peers`,
    :func:`club_members_clearing`, and :func:`most_recent_prior_holder` all
    slice differently. Batched by each baseline row's own ``(season_range,
    min_minutes)`` (normally one shared pair across every cohort from one Job
    A run) -- one query per distinct pair, not per cohort_key and never per
    player.

    Args:
        db: Active database session.
        baselines_by_cohort: The active T1 **event-grain** baseline row for
            every cohort_key this tick needs peers for (see
            :func:`fetch_event_baselines`).

    Returns:
        ``cohort_key -> [CohortMember, ...]`` for every key in
        ``baselines_by_cohort`` (empty list, not a missing key, when nothing
        qualifies). Includes the subject's own historical rows -- callers
        exclude the subject via ``exclude_player_id`` in the slicing helpers
        below.
    """
    if not baselines_by_cohort:
        return {}

    groups: dict[tuple[str, float], set[str]] = defaultdict(set)
    for cohort_key, baseline in baselines_by_cohort.items():
        groups[(baseline.season_range, baseline.min_minutes)].add(cohort_key)

    out: dict[str, list[CohortMember]] = {key: [] for key in baselines_by_cohort}
    for (season_range, min_minutes), keys_in_group in groups.items():
        start_year, end_year = _parse_season_range(season_range)
        stmt = select(  # type: ignore[call-overload]
            SummerLeaguePlayerSeason.player_id,
            SummerLeaguePlayerSeason.year,
            SummerLeaguePlayerSeason.gmsc,
            SummerLeaguePlayerSeason.minutes,
            SummerLeaguePlayerSeason.gp,
        ).where(
            SummerLeaguePlayerSeason.year >= start_year,
            SummerLeaguePlayerSeason.year <= end_year,
        )
        rows = (await db.execute(stmt)).all()
        events = blend_event_aggregates(rows, min_minutes=min_minutes)
        if not events:
            continue

        player_ids = {pid for pid, _year in events}
        slot_stmt = select(  # type: ignore[call-overload]
            PlayerMaster.id,
            PlayerMaster.draft_round,
            PlayerMaster.draft_pick,
            PlayerMaster.display_name,
        ).where(
            PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
        )
        slot_rows = (await db.execute(slot_stmt)).all()
        meta = {pid: (rnd, pick, name) for pid, rnd, pick, name in slot_rows}

        for (pid, year), agg in events.items():
            rnd, pick, name = meta.get(pid, (None, None, None))
            key = cohort_key_for(rnd, pick, grain=SummerLeagueDeskGrain.EVENT)
            if key not in keys_in_group:
                continue
            out.setdefault(key, []).append(
                CohortMember(
                    player_id=pid,
                    player_label=name or f"Player {pid}",
                    year=year,
                    value=agg.gmsc,
                )
            )
    return out


def cohort_peers(
    members: Sequence[CohortMember], *, exclude_player_id: int
) -> list[CohortPeer]:
    """The rest of a historical cohort population, subject excluded (any year).

    Feeds :func:`~app.services.summer_league.desk_facts.detect_cohort_rank`.
    """
    return [
        CohortPeer(label=m.player_label, value=m.value)
        for m in members
        if m.player_id != exclude_player_id
    ]


def count_club_threshold(
    baseline: SummerLeagueCohortBaseline, *, pctl_key: str = COUNT_CLUB_BREAKPOINT_PCTL
) -> Optional[float]:
    """The cohort's ``count_club``/``first_since`` qualifying bar (see module docstring)."""
    return baseline.breakpoints.get(pctl_key)


def club_members_clearing(
    members: Sequence[CohortMember],
    *,
    exclude_player_id: int,
    threshold: float,
    higher_is_better: bool = True,
) -> list[ClubMember]:
    """Historical peers (subject excluded) who clear ``threshold``.

    Feeds :func:`~app.services.summer_league.desk_facts.detect_count_club`.
    """

    def clears(value: float) -> bool:
        return value >= threshold if higher_is_better else value <= threshold

    return [
        ClubMember(label=m.player_label, value=m.value, year=m.year)
        for m in members
        if m.player_id != exclude_player_id and clears(m.value)
    ]


def most_recent_prior_holder(
    members: Sequence[CohortMember],
    *,
    exclude_player_id: int,
    threshold: float,
    before_year: int,
    higher_is_better: bool = True,
) -> Optional[PriorHolder]:
    """The most recent historical peer (subject excluded) to clear ``threshold``.

    Only considers members strictly before ``before_year`` (the subject's own
    current-event year is never its own "prior"). Ties on year broken by the
    higher (or lower, for ``higher_is_better=False``) value.

    Feeds :func:`~app.services.summer_league.desk_facts.detect_first_since`.
    """

    def clears(value: float) -> bool:
        return value >= threshold if higher_is_better else value <= threshold

    candidates = [
        m
        for m in members
        if m.player_id != exclude_player_id and m.year < before_year and clears(m.value)
    ]
    if not candidates:
        return None
    winner = max(
        candidates, key=lambda m: (m.year, m.value if higher_is_better else -m.value)
    )
    return PriorHolder(label=winner.player_label, value=winner.value, year=winner.year)


# --------------------------------------------------------------------------- #
# T1 baseline lookups (event-grain peers/threshold source; debut_vs_bar)
# --------------------------------------------------------------------------- #
async def _fetch_baselines(
    db: AsyncSession,
    *,
    baseline_version: str,
    cohort_keys: Sequence[str],
    grain: SummerLeagueDeskGrain,
) -> dict[str, SummerLeagueCohortBaseline]:
    if not cohort_keys:
        return {}
    stmt = select(SummerLeagueCohortBaseline).where(
        SummerLeagueCohortBaseline.baseline_version == baseline_version,  # type: ignore[arg-type]
        SummerLeagueCohortBaseline.cohort_key.in_(cohort_keys),  # type: ignore[attr-defined]
        SummerLeagueCohortBaseline.grain == grain,  # type: ignore[arg-type]
        SummerLeagueCohortBaseline.is_active.is_(True),  # type: ignore[attr-defined]
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {row.cohort_key: row for row in rows}


async def fetch_event_baselines(
    db: AsyncSession, *, baseline_version: str, cohort_keys: Sequence[str]
) -> dict[str, SummerLeagueCohortBaseline]:
    """The active T1 **event-grain** baseline row for each of ``cohort_keys``.

    One batched query for every cohort_key this tick's graded players span
    (never one per player) -- the shared source for
    :func:`fetch_cohort_members`'s ``season_range``/``min_minutes`` and the
    ``count_club``/``first_since`` threshold (:func:`count_club_threshold`).
    """
    return await _fetch_baselines(
        db,
        baseline_version=baseline_version,
        cohort_keys=cohort_keys,
        grain=SummerLeagueDeskGrain.EVENT,
    )


async def fetch_debut_baselines(
    db: AsyncSession, *, baseline_version: str, cohort_keys: Sequence[str]
) -> dict[str, SummerLeagueCohortBaseline]:
    """The active T1 **debut-grain** baseline row for each of ``cohort_keys``.

    ``cohort_keys`` here are ``debut:``-prefixed (`cohort_baselines.cohort_key_for`
    with ``grain=SummerLeagueDeskGrain.DEBUT`). Feeds
    :func:`~app.services.summer_league.desk_facts.detect_debut_vs_bar`'s
    ``debut_bar`` (the row's ``mean_value``, spec §6 "Debut bar = cohort mean").
    """
    return await _fetch_baselines(
        db,
        baseline_version=baseline_version,
        cohort_keys=cohort_keys,
        grain=SummerLeagueDeskGrain.DEBUT,
    )


async def fetch_game_baselines(
    db: AsyncSession, *, baseline_version: str, cohort_keys: Sequence[str]
) -> dict[str, SummerLeagueCohortBaseline]:
    """The active T1 **game-grain** baseline row for each of ``cohort_keys``.

    ``cohort_keys`` here are ``game:``-prefixed (`cohort_baselines.cohort_key_for`
    with ``grain=SummerLeagueDeskGrain.GAME`). Feeds :func:`fetch_game_lines`'s
    ``baseline_by_player`` and :func:`~app.services.summer_league.desk_facts.detect_streak`'s
    per-game percentile bar (#525 -- the correct single-game distribution,
    replacing the event-grain approximation earlier tickets shipped).
    """
    return await _fetch_baselines(
        db,
        baseline_version=baseline_version,
        cohort_keys=cohort_keys,
        grain=SummerLeagueDeskGrain.GAME,
    )


# --------------------------------------------------------------------------- #
# Tonight's live field (leads_field)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldEntry:
    """One tracked player's single-game GmSc for tonight's live field."""

    player_id: int
    player_label: str
    value: float


async def fetch_tonight_field(
    db: AsyncSession, *, competition_id: int, game_date: date
) -> list[FieldEntry]:
    """Every tracked player who logged a game for ``competition_id`` on ``game_date``.

    One query for the whole night, not per player -- ``value`` is that
    single game's GmSc (`game_score_from_row`, spec §1's "live GmSc" grain:
    "the highest-GmSc tracked player in that game so far"), which is why this
    is a distinct fetch from the event-aggregate ``subject_value`` T2 grades
    carry. A player with more than one logged game on ``game_date`` (multiple
    venues on one calendar day) contributes their better line, so the field
    reflects one entry per player.

    Feeds :func:`~app.services.summer_league.desk_facts.detect_leads_field`.

    Returns:
        One :class:`FieldEntry` per distinct player who played on
        ``game_date``, unordered.
    """
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeaguePlayerGameLog, PlayerMaster.display_name
        )
        .join(
            SummerLeagueGame, SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id
        )
        .join(PlayerMaster, PlayerMaster.id == SummerLeaguePlayerGameLog.player_id)  # type: ignore[arg-type]
        .where(
            SummerLeaguePlayerGameLog.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueGame.game_date == game_date,  # type: ignore[arg-type]
        )
    )
    rows = (await db.execute(stmt)).all()

    best_by_player: dict[int, FieldEntry] = {}
    for log_row, display_name in rows:
        if log_row.player_id is None:
            continue
        gmsc = round(game_score_from_row(log_row), 2)
        current = best_by_player.get(log_row.player_id)
        if current is None or gmsc > current.value:
            best_by_player[log_row.player_id] = FieldEntry(
                player_id=log_row.player_id,
                player_label=display_name or f"Player {log_row.player_id}",
                value=gmsc,
            )
    return list(best_by_player.values())


def field_peers(
    entries: Sequence[FieldEntry], *, exclude_player_id: int
) -> list[CohortPeer]:
    """The rest of tonight's field, subject excluded."""
    return [
        CohortPeer(label=e.player_label, value=e.value)
        for e in entries
        if e.player_id != exclude_player_id
    ]


# --------------------------------------------------------------------------- #
# Prior SL event / debut status (self_delta, debut_vs_bar gate)
# --------------------------------------------------------------------------- #
async def fetch_prior_events(
    db: AsyncSession, *, player_ids: Sequence[int], before_year: int
) -> dict[int, PriorEvent]:
    """Each player's most recent blended SL event strictly before ``before_year``.

    One batched query for every player_id (mirrors
    ``desk_storylines._prior_event``'s per-player blend, applied here across
    the whole roster at once). Feeds
    :func:`~app.services.summer_league.desk_facts.detect_self_delta`.

    Returns:
        ``player_id -> PriorEvent`` only for players with at least one
        qualifying prior-year event; debutants are simply absent.
    """
    if not player_ids:
        return {}
    stmt = select(  # type: ignore[call-overload]
        SummerLeaguePlayerSeason.player_id,
        SummerLeaguePlayerSeason.year,
        SummerLeaguePlayerSeason.gmsc,
        SummerLeaguePlayerSeason.minutes,
        SummerLeaguePlayerSeason.gp,
    ).where(
        SummerLeaguePlayerSeason.player_id.in_(player_ids),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.year < before_year,
    )
    rows = (await db.execute(stmt)).all()
    events = blend_event_aggregates(rows, min_minutes=0.0)

    latest_by_player: dict[int, tuple[int, float, int]] = {}
    for (pid, year), agg in events.items():
        current = latest_by_player.get(pid)
        if current is None or year > current[0]:
            latest_by_player[pid] = (year, agg.gmsc, agg.gp)

    return {
        pid: PriorEvent(year=year, value=value, gp=gp)
        for pid, (year, value, gp) in latest_by_player.items()
    }


async def fetch_current_event_gp(
    db: AsyncSession, *, player_ids: Sequence[int], year: int
) -> dict[int, int]:
    """Each player's total games played across every venue in ``year``.

    One batched query (mirrors ``desk_storylines._current_event_gp``, summed
    per player instead of one call per player). Feeds the ``current_gp``
    argument of :func:`~app.services.summer_league.desk_facts.detect_self_delta`.
    """
    if not player_ids:
        return {}
    stmt = select(
        SummerLeaguePlayerSeason.player_id, SummerLeaguePlayerSeason.gp
    ).where(  # type: ignore[call-overload]
        SummerLeaguePlayerSeason.player_id.in_(player_ids),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.year == year,
    )
    rows = (await db.execute(stmt)).all()
    out: dict[int, int] = defaultdict(int)
    for pid, gp in rows:
        out[pid] += int(gp or 0)
    return dict(out)


async def fetch_first_qualifying_games(
    db: AsyncSession,
    *,
    player_ids: Sequence[int],
    min_minutes: float = DEFAULT_GAME_MIN_MINUTES,
) -> dict[int, FirstQualifyingGame]:
    """The ONE batched ``player_id -> first-qualifying-game`` lookup (#539).

    Every read path that needs to know "which single game was this player's
    debut" -- the storyline debut trigger
    (`desk_storylines.compute_desk_storylines`, which fires only when
    tonight's ``game_id`` matches) and the fact path's debut-status check
    (:func:`fetch_debut_status`, below) -- shares this ONE query and the
    SAME reduction (`cohort_baselines.first_qualifying_games`) Job A's
    ``debut`` grain builds from. Scoped to every game a player has EVER
    logged (not bounded by any one competition's ``season_range``), since a
    player's real debut can predate whatever window a given tick's Job A
    baseline happens to cover.

    Args:
        db: Active database session.
        player_ids: Every player to resolve a first-qualifying-game for.
        min_minutes: The per-game eligibility gate (mirrors
            `cohort_baselines.DEFAULT_GAME_MIN_MINUTES`).

    Returns:
        ``player_id -> FirstQualifyingGame``. A player with no qualifying
        game logged yet is simply absent.
    """
    if not player_ids:
        return {}
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeaguePlayerGameLog, SummerLeagueGame.game_date
        )
        .join(
            SummerLeagueGame, SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id
        )
        .where(
            SummerLeaguePlayerGameLog.player_id.in_(player_ids),  # type: ignore[union-attr]
        )
    )
    rows = (await db.execute(stmt)).all()
    return first_qualifying_games(rows, min_minutes=min_minutes)


async def fetch_debut_status(
    db: AsyncSession, *, player_ids: Sequence[int], before_year: int
) -> dict[int, bool]:
    """Whether each player's canonical first-qualifying game (#539) predates ``before_year``.

    Reuses :func:`fetch_first_qualifying_games` -- the SAME shared lookup the
    storyline debut trigger fires against -- rather than an independently
    derived "has a prior year of SL history" check, so the fact path's
    ``debut_vs_bar`` Fact and the storyline path's Debut trigger always agree
    on which single game counted as a player's debut. ``True`` means the
    player is debuting this event -- feeds the caller's decision to call
    :func:`~app.services.summer_league.desk_facts.detect_debut_vs_bar`.

    Args:
        db: Active database session.
        player_ids: Every player to resolve debut status for.
        before_year: A player whose first qualifying game falls in a year
            strictly before this one has already debuted; anything else
            (including no qualifying game on record at all yet) reads as a
            debutant.

    Returns:
        ``player_id -> is_debut`` for every id in ``player_ids``.
    """
    if not player_ids:
        return {}
    first_games = await fetch_first_qualifying_games(db, player_ids=player_ids)
    out: dict[int, bool] = {}
    for pid in player_ids:
        fqg = first_games.get(pid)
        if fqg is None or fqg.game_date is None:
            out[pid] = True
            continue
        out[pid] = fqg.game_date.year >= before_year
    return out


# --------------------------------------------------------------------------- #
# Per-game GmSc log this event, game-grain pctl (streak)
# --------------------------------------------------------------------------- #
async def fetch_game_lines(
    db: AsyncSession,
    *,
    player_ids: Sequence[int],
    competition_id: int,
    game_date: date,
    baseline_by_player: Mapping[int, Optional[SummerLeagueCohortBaseline]],
    inclusive: bool = True,
) -> dict[int, list[GameLine]]:
    """Each player's chronological GmSc log this competition, through/before ``game_date``.

    ONE batched query for the whole roster (never per-player) -- shared by
    two callers with different date-boundary needs, both formerly per-player
    loops (#548): this module's own tick-time streak fact
    (:func:`~app.services.summer_league.desk_facts.detect_streak` via
    ``desk_fact_queries``' caller, ``inclusive=True``, "as of tonight") and
    ``desk_storylines.compute_desk_storylines``'s entering-weight streak
    trigger (``inclusive=False``, strictly *before* tonight -- the pre-#548
    per-player ``desk_storylines._game_lines_before`` this replaces). Each
    player's line is scored against *their own* cohort's **game-grain**
    baseline (``baseline_by_player`` -- see :func:`fetch_game_baselines`),
    since different players can carry different ``cohort_key``s in the same
    tick.

    Args:
        db: Active database session.
        player_ids: Every player to fetch a log for.
        competition_id: Scopes the game log to one event.
        game_date: The date boundary; see ``inclusive``.
        inclusive: ``True`` (default) includes ``game_date`` itself -- an "as
            of tonight" log. ``False`` excludes it -- an "entering" (strictly
            pre-tonight) log, e.g. for a Morning/pre-tip streak evaluation
            where tonight's own game hasn't happened yet.
        baseline_by_player: Each player's active T1 **game-grain** baseline
            row (or ``None`` -- that player's lines get ``pctl=None``,
            stopping any streak through them per `desk_facts.detect_streak`).

    Returns:
        ``player_id -> [GameLine, ...]``, oldest first, only for players with
        at least one logged game.
    """
    if not player_ids:
        return {}
    cutoff = game_date + timedelta(days=1) if inclusive else game_date
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeaguePlayerGameLog, SummerLeagueGame.game_date
        )
        .join(
            SummerLeagueGame, SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id
        )
        .where(
            SummerLeaguePlayerGameLog.player_id.in_(player_ids),  # type: ignore[union-attr]
            SummerLeaguePlayerGameLog.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueGame.game_date < cutoff,  # type: ignore[operator]
        )
        .order_by(
            SummerLeaguePlayerGameLog.player_id.asc(),  # type: ignore[union-attr]
            SummerLeagueGame.game_date.asc(),  # type: ignore[union-attr]
        )
    )
    rows = (await db.execute(stmt)).all()

    out: dict[int, list[GameLine]] = defaultdict(list)
    for log_row, g_date in rows:
        if log_row.player_id is None:
            continue
        baseline = baseline_by_player.get(log_row.player_id)
        gmsc = round(game_score_from_row(log_row), 2)
        pctl: Optional[float] = None
        cohort_median = 0.0
        if baseline is not None:
            cohort_median = baseline.median_value
            try:
                pctl = percentile_of_value(baseline.breakpoints, gmsc)
            except ValueError:
                pctl = None
        out[log_row.player_id].append(
            GameLine(
                value=gmsc,
                cohort_median=cohort_median,
                pctl=pctl,
                game_id=log_row.game_id,
                label=g_date.isoformat() if g_date else None,
            )
        )
    return dict(out)


__all__ = [
    "COUNT_CLUB_BREAKPOINT_PCTL",
    "CohortMember",
    "FieldEntry",
    "club_members_clearing",
    "cohort_peers",
    "count_club_threshold",
    "field_peers",
    "fetch_cohort_members",
    "fetch_current_event_gp",
    "fetch_debut_baselines",
    "fetch_debut_status",
    "fetch_event_baselines",
    "fetch_first_qualifying_games",
    "fetch_game_baselines",
    "fetch_game_lines",
    "fetch_prior_events",
    "fetch_tonight_field",
    "most_recent_prior_holder",
]

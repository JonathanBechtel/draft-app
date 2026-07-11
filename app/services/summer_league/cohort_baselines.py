"""Job A — the Summer League Desk cohort-baseline builder (T1).

Offline, rare job (`docs/plans/summer-league-scouts-desk-behavior-spec.md` §6,
§10): reads 2017-2025 Summer League history, groups every player-year event
into a draft-slot/status cohort per the window rule, and writes a new
versioned distribution to ``summer_league_cohort_baselines`` (T1) —
breakpoints, mean, median, sample size — flipping ``is_active`` so the hourly
tick (Job B, a separate ticket) always ranks against exactly one active
version per cohort. Never rebuilt on the tick; T1 is the expensive-but-stable
artifact.

**Naming:** this module is the *slot-cohort baseline* (draft-slot comparison
group). ``app.services.summer_league.cohort`` is a different thing — the
*roster* cohort (which players are actively rostered for a competition). Keep
the two apart.

Window rule (behavior spec §6, restated against this repo's actual column
semantics — ``players_master.draft_pick`` is **within-round**, not overall):

* **Lottery** — ``draft_round == 1 and draft_pick <= 14``. Cohort =
  ``slot:{low}-{high}``, a ±3-pick window simply clamped to ``[1, 14]``
  (no re-centering when the window would clip past an edge — pick 1's window
  is ``[1, 4]``, pick 14's is ``[11, 14]``, pick 5's is ``[2, 8]``).
* **Round 1, non-lottery** — ``draft_round == 1 and draft_pick in [15, 30]``
  (within-round == overall for round 1). Cohort = ``round:1_late``.
* **Round 2** — ``draft_round == 2`` (any within-round pick 1-30, i.e. overall
  picks 31-60). Cohort = ``round:2``.
* **Undrafted** — no ``draft_round``/``draft_pick`` on record. Cohort =
  ``status:undrafted``.

``slot_low``/``slot_high`` on the persisted row always store the human-facing
**overall** draft-position bounds (so ``round:2`` stores ``31-60`` even though
the membership test uses the within-round column), except for the status
cohort where both are ``None``.

**Grain:** builds all three ``SummerLeagueDeskGrain`` values:

* ``event`` — one data point per (player, year): that player's SL-season
  event-aggregate GmSc, games-weighted across every venue they played that
  year (mirrors the blend approach in ``get_blended_leaders`` /
  ``_blend_leader_values``). A player who returns for a second summer
  contributes one event per year to their (fixed) slot cohort.
  ``cohort_key`` uses the ``slot:``/``round:``/``status:`` prefix.
  ``cohort_kind`` is ``slot_window``/``round_bucket``/``status``.
* ``debut`` — one data point per player: their single, chronologically
  **earliest qualifying individual game** within ``season_range`` (its raw
  GmSc via :func:`~app.services.summer_league.metrics.game_score_from_row`),
  gated by the SAME per-game minutes floor the ``game`` grain uses
  (:data:`DEFAULT_GAME_MIN_MINUTES`) — not the event grain's blended-season
  aggregate for a player's earliest *year* (the pre-#539 approach, which let
  a low-minutes cameo count as a "debut" and had no game-level anchor a
  trigger could compare against). :func:`first_qualifying_games` is this
  grain's single source of truth, shared with the storyline debut trigger's
  firing condition (`desk_storylines.py`) and the fact path's debut-status
  lookup (`desk_fact_queries.fetch_first_qualifying_games`) — one canonical
  "what game was this player's debut" definition, not three. ``cohort_key``
  mirrors the same window/bucket suffix but under the ``debut:`` prefix
  (e.g. ``debut:1-4``, ``debut:1_late``, ``debut:undrafted``), and
  ``cohort_kind`` is always ``debut`` regardless of which underlying
  slot/round/status window it represents — that dedicated kind is how a
  debut-grain row is told apart from an event-grain row sharing the same
  slot window. Persisted with ``min_minutes`` set to the per-game floor
  (:data:`DEFAULT_GAME_MIN_MINUTES`), not the event grain's blended-season
  floor, since that's the gate actually applied to this grain's members.
* ``game`` — one data point per **individual game log line**: a player's raw
  per-game GmSc (``game_score_from_row``), gated by a **per-game** minutes
  floor (:data:`DEFAULT_GAME_MIN_MINUTES`) rather than the event grain's
  blended season-level floor (:data:`DEFAULT_MIN_MINUTES`) — a single
  qualifying game only needs a meaningful run, not a whole summer's worth of
  minutes. Unlike ``event`` (every same-year row blended into ONE point per
  player), ``game`` pools every qualifying game log line ungrouped, so a
  player who logs five qualifying games in a summer contributes five points
  to the distribution. ``cohort_key`` uses the ``game:`` prefix (mirrors the
  ``debut:`` convention) so a game-grain row never collides with its
  cohort's event-grain row under the same ``baseline_version`` — the T1
  table's ``(baseline_version, cohort_key)`` uniqueness needs the grain
  distinguished in the key itself, not just the ``grain`` column.
  ``cohort_kind`` follows the same window/bucket kind as ``event``
  (``slot_window``/``round_bucket``/``status``) — ``game`` is a different
  measurement grain of the identical draft-slot classification, not a
  distinct *kind* of cohort the way ``debut`` is.

Added by #525 to replace the ``streak`` trigger's event-grain approximation
(``desk_storylines.py`` / ``desk_facts.py`` / ``desk_fact_queries.py`` used to
rank one game's GmSc against the event-aggregate distribution — a real but
documented approximation, since event aggregates have much lower variance
than individual games and stretch percentiles toward the tails) with the
correct single-game distribution.

#539 carried the same game-grain fix into the ``debut`` grain and the
Ledger's single-game percentile (`desk_read._assemble_ledger`), and
introduced :func:`first_qualifying_games` — the one shared
``player_id -> earliest-qualifying-game`` reduction the debut grain, the
storyline debut trigger, and the fact path's debut-status lookup all read
instead of three independent (and previously inconsistent) definitions of
"debut."
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.metrics import game_score_from_row

# --------------------------------------------------------------------------- #
# Window rule
# --------------------------------------------------------------------------- #
LOTTERY_WINDOW = 3
LOTTERY_MAX_PICK = 14
ROUND1_LATE_LOW, ROUND1_LATE_HIGH = 15, 30
ROUND2_LOW, ROUND2_HIGH = 31, 60

DEFAULT_SEASON_RANGE = "2017-2025"
DEFAULT_MIN_MINUTES = 40.0

# Per-game (not blended-season) eligibility floor for the `game` grain —
# deliberately much lower than DEFAULT_MIN_MINUTES: a single qualifying game
# only needs a meaningful run (not a token cameo), never a whole summer's
# worth of minutes. A documented judgment call (ticket #525), not a spec-pinned
# number — the behavior spec doesn't prescribe a single-game methodology.
DEFAULT_GAME_MIN_MINUTES = 10.0

# Percentile keys the breakpoints map is fit at (0-100 step 5 — dense enough
# for O(1) percentile lookups via linear interpolation, matching numpy's
# default 'linear' method).
DEFAULT_BREAKPOINT_PERCENTILES: tuple[int, ...] = tuple(range(0, 101, 5))


def slot_window(pick: int) -> tuple[int, int]:
    """The ±3-pick lottery window for ``pick``, clamped to ``[1, 14]``.

    Args:
        pick: A lottery draft pick (1-14, within-round == overall for round 1).

    Returns:
        ``(low, high)`` inclusive bounds. Simple clamp, not re-centered: pick
        1 gets ``(1, 4)`` (only 4 wide), pick 5 gets the full ``(2, 8)``.
    """
    return max(1, pick - LOTTERY_WINDOW), min(LOTTERY_MAX_PICK, pick + LOTTERY_WINDOW)


def _bucket(
    draft_round: Optional[int], draft_pick: Optional[int]
) -> tuple[str, Optional[tuple[int, int]], SummerLeagueDeskCohortKind]:
    """Classify a draft slot into its non-debut cohort key suffix + bounds + kind.

    ``draft_pick`` is WITHIN-ROUND in this codebase — a lottery pick is
    ``draft_round == 1 and draft_pick <= 14``, never ``draft_pick`` alone
    (that would wrongly sweep in every 2nd-round pick 1-14).
    """
    if draft_round == 1 and draft_pick is not None and draft_pick <= LOTTERY_MAX_PICK:
        low, high = slot_window(draft_pick)
        return f"{low}-{high}", (low, high), SummerLeagueDeskCohortKind.SLOT_WINDOW
    if draft_round == 1 and draft_pick is not None:
        return (
            "1_late",
            (ROUND1_LATE_LOW, ROUND1_LATE_HIGH),
            SummerLeagueDeskCohortKind.ROUND_BUCKET,
        )
    if draft_round == 2 and draft_pick is not None:
        return "2", (ROUND2_LOW, ROUND2_HIGH), SummerLeagueDeskCohortKind.ROUND_BUCKET
    return "undrafted", None, SummerLeagueDeskCohortKind.STATUS


def cohort_kind_for(
    draft_round: Optional[int],
    draft_pick: Optional[int],
    grain: SummerLeagueDeskGrain = SummerLeagueDeskGrain.EVENT,
) -> SummerLeagueDeskCohortKind:
    """The T1 ``cohort_kind`` for a player's draft slot at the given grain."""
    if grain == SummerLeagueDeskGrain.DEBUT:
        return SummerLeagueDeskCohortKind.DEBUT
    return _bucket(draft_round, draft_pick)[2]


def cohort_key_for(
    draft_round: Optional[int],
    draft_pick: Optional[int],
    grain: SummerLeagueDeskGrain = SummerLeagueDeskGrain.EVENT,
) -> str:
    """The T1 ``cohort_key`` string for a player's draft slot at the given grain.

    Args:
        draft_round: ``players_master.draft_round`` (``None`` when undrafted).
        draft_pick: ``players_master.draft_pick`` — WITHIN-ROUND, not overall.
        grain: ``event`` (default) uses the ``slot:``/``round:``/``status:``
            prefix; ``debut`` always uses the ``debut:`` prefix over the same
            suffix (e.g. ``slot:1-4`` -> ``debut:1-4``); ``game`` always uses
            the ``game:`` prefix over the same suffix (e.g.
            ``slot:1-4`` -> ``game:1-4``) — mirrors the ``debut:`` convention
            so a game-grain row's key never collides with its cohort's
            event-grain row under the same ``baseline_version``.

    Returns:
        e.g. ``"slot:1-4"``, ``"round:1_late"``, ``"round:2"``,
        ``"status:undrafted"``, ``"debut:1-4"``, ``"game:1-4"``.
    """
    suffix, _bounds, _kind = _bucket(draft_round, draft_pick)
    if grain == SummerLeagueDeskGrain.DEBUT:
        return f"debut:{suffix}"
    if grain == SummerLeagueDeskGrain.GAME:
        return f"game:{suffix}"
    prefix = {
        SummerLeagueDeskCohortKind.SLOT_WINDOW: "slot",
        SummerLeagueDeskCohortKind.ROUND_BUCKET: "round",
        SummerLeagueDeskCohortKind.STATUS: "status",
    }[_kind]
    return f"{prefix}:{suffix}"


def slot_bounds_for(
    draft_round: Optional[int], draft_pick: Optional[int]
) -> Optional[tuple[int, int]]:
    """The human-facing overall draft-position bounds for a player's slot.

    ``None`` for the undrafted/status cohort. Used for both the event-grain
    and debut-grain rows of the same underlying window/bucket.
    """
    return _bucket(draft_round, draft_pick)[1]


# --------------------------------------------------------------------------- #
# Distribution math (pure)
# --------------------------------------------------------------------------- #
def compute_breakpoints(
    values: Sequence[float],
    percentiles: Sequence[int] = DEFAULT_BREAKPOINT_PERCENTILES,
) -> dict[str, float]:
    """Percentile -> value map via linear interpolation over ``values``.

    Matches numpy's default ``'linear'`` percentile method so the resulting
    breakpoints map is a robust O(1) lookup table for percentile-of-value
    (or value-of-percentile) queries downstream.

    Args:
        values: The cohort's distribution (unsorted, any order).
        percentiles: Integer percentile keys (0-100) to fit breakpoints at.

    Returns:
        ``{"0": ..., "5": ..., ..., "100": ...}``, rounded to 2 decimals.
        Empty when ``values`` is empty.
    """
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)
    out: dict[str, float] = {}
    for p in percentiles:
        if n == 1:
            out[str(p)] = round(ordered[0], 2)
            continue
        rank = (p / 100.0) * (n - 1)
        lo = int(rank)
        hi = min(lo + 1, n - 1)
        frac = rank - lo
        out[str(p)] = round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 2)
    return out


def compute_mean(values: Sequence[float]) -> float:
    """Mean of ``values``, ``0.0`` for an empty distribution."""
    return round(sum(values) / len(values), 2) if values else 0.0


def compute_median(values: Sequence[float]) -> float:
    """Median of ``values``, ``0.0`` for an empty distribution."""
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 2)


# --------------------------------------------------------------------------- #
# Event-aggregate blending
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EventAggregate:
    """One player's blended (all-venue) SL event for a single year."""

    player_id: int
    year: int
    gmsc: float
    minutes: float
    gp: int


def blend_event_aggregates(
    rows: Sequence[Any], *, min_minutes: float
) -> dict[tuple[int, int], EventAggregate]:
    """Blend same-year, cross-venue ``SummerLeaguePlayerSeason`` rows into events.

    Reuses the games-weighted GmSc blend from
    ``summer_league_metrics_service._blend_leader_values`` (GmSc is a
    per-game score, so it's blended games-weighted, not summed) applied
    within a ``(player_id, year)`` group instead of across a player's whole
    career, matching the "event-aggregate" grain the behavior spec (§6)
    calls for.

    Args:
        rows: Objects (ORM rows or any object) exposing ``player_id``,
            ``year``, ``gmsc``, ``minutes``, ``gp``.
        min_minutes: The eligibility gate — an event whose blended minutes
            fall below this is dropped entirely (never enters the cohort
            distribution).

    Returns:
        ``{(player_id, year): EventAggregate}`` for events that pass the gate
        and have at least one non-null GmSc row.
    """
    buckets: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for r in rows:
        buckets[(r.player_id, r.year)].append(r)

    out: dict[tuple[int, int], EventAggregate] = {}
    for key, group in buckets.items():
        minutes = sum(float(r.minutes or 0.0) for r in group)
        if minutes < min_minutes:
            continue
        gmsc_pairs = [
            (float(r.gmsc), float(r.gp or 0)) for r in group if r.gmsc is not None
        ]
        weight = sum(w for _v, w in gmsc_pairs)
        if weight <= 0:
            continue
        gmsc = sum(v * w for v, w in gmsc_pairs) / weight
        gp = sum(int(r.gp or 0) for r in group)
        out[key] = EventAggregate(
            player_id=key[0],
            year=key[1],
            gmsc=round(gmsc, 2),
            minutes=round(minutes, 1),
            gp=gp,
        )
    return out


def _parse_season_range(season_range: str) -> tuple[int, int]:
    """Split ``"2017-2025"`` into ``(2017, 2025)``."""
    start_str, _sep, end_str = season_range.partition("-")
    return int(start_str), int(end_str)


# --------------------------------------------------------------------------- #
# Game-grain (single individual-game GmSc, per-game minutes floor)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GameValue:
    """One player's single individual-game GmSc — the ``game`` grain's raw data point."""

    player_id: int
    game_id: int
    gmsc: float
    minutes: float


def qualifying_game_values(
    rows: Sequence[Any], *, min_minutes: float
) -> list[GameValue]:
    """Individual per-game GmSc values gated by a per-game minutes floor.

    The ``game``-grain twin of :func:`blend_event_aggregates`: where that
    function collapses every same-year row into ONE event-aggregate data
    point per player, this keeps every qualifying game log line as its own
    ungrouped data point — the correct grain for a per-game cohort
    distribution. Reuses :func:`~app.services.summer_league.metrics.game_score_from_row`
    (the single source of GmSc every other read surface funnels through)
    rather than recomputing anything.

    Args:
        rows: Objects (ORM rows or any object) exposing ``player_id``,
            ``game_id``, ``minutes_seconds``, and the twelve
            ``game_score_line`` box-score fields (missing/``None``
            coalesce to 0 via ``game_score_from_row``).
        min_minutes: The per-game eligibility gate — deliberately a
            *per-game* floor (:data:`DEFAULT_GAME_MIN_MINUTES`), not the
            event grain's blended season-level floor
            (:data:`DEFAULT_MIN_MINUTES`): a single qualifying game only
            needs a meaningful run, not a whole summer's worth of minutes.

    Returns:
        One :class:`GameValue` per row clearing ``min_minutes``.
    """
    out: list[GameValue] = []
    for r in rows:
        minutes = round(float(getattr(r, "minutes_seconds", 0) or 0) / 60.0, 1)
        if minutes < min_minutes:
            continue
        out.append(
            GameValue(
                player_id=r.player_id,
                game_id=r.game_id,
                gmsc=round(game_score_from_row(r), 2),
                minutes=minutes,
            )
        )
    return out


@dataclass(frozen=True)
class FirstQualifyingGame:
    """A player's single, chronologically earliest qualifying individual game.

    The canonical "debut game" (#539): both the ``debut`` grain's raw data
    point (``gmsc``) and the debut-firing condition every read path checks
    (``game_id``) trace back to this ONE reduction — there is no second,
    independently-derived definition of "a player's debut" anywhere else in
    the Desk.
    """

    player_id: int
    game_id: int
    gmsc: float
    game_date: Optional[date]


def _first_qualifying_sort_key(
    game_id: int, game_date: Optional[date]
) -> tuple[bool, Any, int]:
    """Chronological sort key: a missing date always sorts last (never "first")."""
    return (game_date is None, game_date, game_id)


def first_qualifying_games(
    rows: Sequence[tuple[Any, Optional[date]]],
    *,
    min_minutes: float = DEFAULT_GAME_MIN_MINUTES,
) -> dict[int, FirstQualifyingGame]:
    """``player_id -> that player's chronologically first qualifying game`` (#539).

    The ONE shared reduction this ticket introduces: Job A's ``debut`` grain
    (:func:`build_baselines`) uses each returned row's ``gmsc`` as that
    player's single debut data point; the storyline/fact-query read paths
    (`desk_storylines.compute_desk_storylines`,
    `desk_fact_queries.fetch_first_qualifying_games`) use each row's
    ``game_id`` to decide whether a specific game IS the subject's debut --
    never "no prior-*year* log," the pre-#539 approximation that fired a
    debut trigger on every game of a player's debut season instead of just
    the first one.

    Applies the same per-game minutes gate :func:`qualifying_game_values`
    uses (:data:`DEFAULT_GAME_MIN_MINUTES` by default) row-by-row, then keeps
    only the earliest-``game_date`` qualifying row per player. Ties (a
    same-day doubleheader) break on the lower ``game_id`` for determinism. A
    row with no ``game_date`` (legacy pre-scoreboard-ingest data) sorts last,
    never winning over a dated row.

    Args:
        rows: ``(log_row, game_date)`` pairs -- ``log_row`` exposes
            ``player_id``, ``game_id``, ``minutes_seconds``, and the
            box-score fields :func:`~app.services.summer_league.metrics.game_score_from_row`
            needs (a ``SummerLeaguePlayerGameLog`` row joined with its game's
            ``SummerLeagueGame.game_date``); ``game_date`` is that game's
            date, possibly ``None``.
        min_minutes: The per-game eligibility gate.

    Returns:
        ``player_id -> FirstQualifyingGame``. A player with no qualifying
        game among ``rows`` is simply absent -- they haven't debuted yet
        under this gate, within the rows supplied.
    """
    best: dict[int, FirstQualifyingGame] = {}
    for log_row, game_date in rows:
        player_id = log_row.player_id
        if player_id is None:
            continue
        minutes = round(float(getattr(log_row, "minutes_seconds", 0) or 0) / 60.0, 1)
        if minutes < min_minutes:
            continue
        candidate = FirstQualifyingGame(
            player_id=player_id,
            game_id=log_row.game_id,
            gmsc=round(game_score_from_row(log_row), 2),
            game_date=game_date,
        )
        current = best.get(player_id)
        if current is None or _first_qualifying_sort_key(
            candidate.game_id, candidate.game_date
        ) < _first_qualifying_sort_key(current.game_id, current.game_date):
            best[player_id] = candidate
    return best


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def build_baselines(
    db: AsyncSession,
    *,
    season_range: str = DEFAULT_SEASON_RANGE,
    min_minutes: float = DEFAULT_MIN_MINUTES,
    game_min_minutes: float = DEFAULT_GAME_MIN_MINUTES,
) -> str:
    """Build a new versioned T1 baseline set from SL history and activate it.

    Reads every ``SummerLeaguePlayerSeason`` row within ``season_range``,
    blends each player's same-year rows into an event-aggregate GmSc (the
    min-minutes gate applied here), and assigns each event to its player's
    slot/round/status cohort (event grain); separately reads every
    ``SummerLeaguePlayerGameLog`` row within the same ``season_range`` and
    assigns each qualifying individual game (the per-game
    ``game_min_minutes`` floor applied here) to the same cohort under the
    ``game`` grain, AND reduces each player's own qualifying games down to
    their single chronologically earliest one (:func:`first_qualifying_games`,
    #539) to build the ``debut`` grain. Computes breakpoints/mean/median per
    cohort per grain,
    and writes every row under one new ``baseline_version`` with
    ``is_active=True``. Every row from every prior version is flipped to
    ``is_active=False`` in the same call — old rows are never deleted or
    mutated otherwise, so a rebuild is always idempotent and non-destructive.

    Does not commit; the caller controls the transaction (mirrors
    ``app.services.summer_league.metrics.rebuild``).

    Args:
        db: Async session.
        season_range: ``"<start>-<end>"`` inclusive year bounds, e.g.
            ``"2017-2025"``.
        min_minutes: Minimum blended minutes for a player-year event to enter
            the event-grain distribution.
        game_min_minutes: Minimum single-game minutes for an individual game
            log line to enter the game grain AND the debut grain (#539 --
            debut is now each player's earliest qualifying *game*, not
            event-year) distributions. A per-game floor, deliberately much
            lower than ``min_minutes``'s blended-season floor.

    Returns:
        The new ``baseline_version`` string.

    Raises:
        ValueError: No qualifying events were found (nothing to build).
    """
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
        raise ValueError(
            f"No qualifying Summer League events in {season_range} "
            f"(min_minutes={min_minutes}); refusing to write an empty baseline_version."
        )

    game_stmt = (
        select(  # type: ignore[call-overload]
            SummerLeaguePlayerGameLog, SummerLeagueGame.game_date
        )
        .join(
            SummerLeagueCompetition,
            SummerLeagueCompetition.id == SummerLeaguePlayerGameLog.competition_id,  # type: ignore[arg-type]
        )
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayerGameLog.player_id.is_not(None),  # type: ignore[union-attr]
            SummerLeagueCompetition.year >= start_year,  # type: ignore[arg-type]
            SummerLeagueCompetition.year <= end_year,  # type: ignore[arg-type]
        )
    )
    game_rows_with_dates = (await db.execute(game_stmt)).all()
    game_rows = [log_row for log_row, _game_date in game_rows_with_dates]
    game_values = qualifying_game_values(game_rows, min_minutes=game_min_minutes)
    # The debut grain's raw data point: each player's single chronologically
    # earliest qualifying game (#539) -- reuses the SAME per-game floor the
    # `game` grain applies, over the SAME rows already fetched above.
    first_qualifying = first_qualifying_games(
        game_rows_with_dates,  # type: ignore[arg-type]
        min_minutes=game_min_minutes,
    )

    player_ids = {pid for pid, _year in events} | {gv.player_id for gv in game_values}
    slot_stmt = select(  # type: ignore[call-overload]
        PlayerMaster.id, PlayerMaster.draft_round, PlayerMaster.draft_pick
    ).where(
        PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
    )
    draft_slot: dict[int, tuple[Optional[int], Optional[int]]] = {
        pid: (rnd, pick) for pid, rnd, pick in (await db.execute(slot_stmt)).all()
    }

    event_values: dict[str, list[float]] = defaultdict(list)
    event_meta: dict[
        str, tuple[Optional[tuple[int, int]], SummerLeagueDeskCohortKind]
    ] = {}

    for (pid, _year), agg in events.items():
        rnd, pick = draft_slot.get(pid, (None, None))
        event_key = cohort_key_for(rnd, pick, grain=SummerLeagueDeskGrain.EVENT)
        _suffix, bounds, kind = _bucket(rnd, pick)
        event_values[event_key].append(agg.gmsc)
        event_meta[event_key] = (bounds, kind)

    # Debut grain (#539): one data point per player -- their earliest
    # qualifying individual game's own GmSc, not a blended season aggregate.
    debut_values: dict[str, list[float]] = defaultdict(list)
    debut_meta: dict[str, Optional[tuple[int, int]]] = {}
    for pid, fqg in first_qualifying.items():
        rnd, pick = draft_slot.get(pid, (None, None))
        suffix, bounds, _kind = _bucket(rnd, pick)
        debut_key = f"debut:{suffix}"
        debut_values[debut_key].append(fqg.gmsc)
        debut_meta[debut_key] = bounds

    game_gmsc_values: dict[str, list[float]] = defaultdict(list)
    game_meta: dict[
        str, tuple[Optional[tuple[int, int]], SummerLeagueDeskCohortKind]
    ] = {}
    for gv in game_values:
        rnd, pick = draft_slot.get(gv.player_id, (None, None))
        _suffix, bounds, kind = _bucket(rnd, pick)
        game_key = cohort_key_for(rnd, pick, grain=SummerLeagueDeskGrain.GAME)
        game_gmsc_values[game_key].append(gv.gmsc)
        game_meta[game_key] = (bounds, kind)

    version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")

    new_rows: list[SummerLeagueCohortBaseline] = []
    for key, values in event_values.items():
        bounds, kind = event_meta[key]
        low, high = bounds if bounds else (None, None)
        new_rows.append(
            SummerLeagueCohortBaseline(
                baseline_version=version,
                is_active=True,
                cohort_key=key,
                cohort_kind=kind,
                slot_low=low,
                slot_high=high,
                metric="gmsc",
                grain=SummerLeagueDeskGrain.EVENT,
                venue_scope="all",
                season_range=season_range,
                min_minutes=min_minutes,
                n_members=len(values),
                breakpoints=compute_breakpoints(values),
                mean_value=compute_mean(values),
                median_value=compute_median(values),
            )
        )
    for key, values in debut_values.items():
        bounds = debut_meta[key]
        low, high = bounds if bounds else (None, None)
        new_rows.append(
            SummerLeagueCohortBaseline(
                baseline_version=version,
                is_active=True,
                cohort_key=key,
                cohort_kind=SummerLeagueDeskCohortKind.DEBUT,
                slot_low=low,
                slot_high=high,
                metric="gmsc",
                grain=SummerLeagueDeskGrain.DEBUT,
                venue_scope="all",
                season_range=season_range,
                min_minutes=game_min_minutes,
                n_members=len(values),
                breakpoints=compute_breakpoints(values),
                mean_value=compute_mean(values),
                median_value=compute_median(values),
            )
        )
    for key, values in game_gmsc_values.items():
        bounds, kind = game_meta[key]
        low, high = bounds if bounds else (None, None)
        new_rows.append(
            SummerLeagueCohortBaseline(
                baseline_version=version,
                is_active=True,
                cohort_key=key,
                cohort_kind=kind,
                slot_low=low,
                slot_high=high,
                metric="gmsc",
                grain=SummerLeagueDeskGrain.GAME,
                venue_scope="all",
                season_range=season_range,
                min_minutes=game_min_minutes,
                n_members=len(values),
                breakpoints=compute_breakpoints(values),
                mean_value=compute_mean(values),
                median_value=compute_median(values),
            )
        )

    # Flip every prior version's rows inactive before activating the new one.
    await db.execute(update(SummerLeagueCohortBaseline).values(is_active=False))
    for row in new_rows:
        db.add(row)
    await db.flush()

    return version

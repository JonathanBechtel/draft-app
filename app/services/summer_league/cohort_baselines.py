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

**Grain:** V1 builds two of the three ``SummerLeagueDeskGrain`` values:

* ``event`` — one data point per (player, year): that player's SL-season
  event-aggregate GmSc, games-weighted across every venue they played that
  year (mirrors the blend approach in ``get_blended_leaders`` /
  ``_blend_leader_values``). A player who returns for a second summer
  contributes one event per year to their (fixed) slot cohort.
  ``cohort_key`` uses the ``slot:``/``round:``/``status:`` prefix.
  ``cohort_kind`` is ``slot_window``/``round_bucket``/``status``.
* ``debut`` — one data point per player: only their **earliest** qualifying
  year within ``season_range``. ``cohort_key`` mirrors the same window/bucket
  suffix but under the ``debut:`` prefix (e.g. ``debut:1-4``,
  ``debut:1_late``, ``debut:undrafted``), and ``cohort_kind`` is always
  ``debut`` regardless of which underlying slot/round/status window it
  represents — that dedicated kind is how a debut-grain row is told apart
  from an event-grain row sharing the same slot window.

``game`` grain is **out of scope for this ticket** — the spec doesn't pin a
single-game cohort methodology and it isn't required by #502's Definition of
Done; a future ticket can add it once a detector needs it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason

# --------------------------------------------------------------------------- #
# Window rule
# --------------------------------------------------------------------------- #
LOTTERY_WINDOW = 3
LOTTERY_MAX_PICK = 14
ROUND1_LATE_LOW, ROUND1_LATE_HIGH = 15, 30
ROUND2_LOW, ROUND2_HIGH = 31, 60

DEFAULT_SEASON_RANGE = "2017-2025"
DEFAULT_MIN_MINUTES = 40.0

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
            suffix (e.g. ``slot:1-4`` -> ``debut:1-4``).

    Returns:
        e.g. ``"slot:1-4"``, ``"round:1_late"``, ``"round:2"``,
        ``"status:undrafted"``, ``"debut:1-4"``.
    """
    suffix, _bounds, _kind = _bucket(draft_round, draft_pick)
    if grain == SummerLeagueDeskGrain.DEBUT:
        return f"debut:{suffix}"
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
# Orchestration
# --------------------------------------------------------------------------- #
async def build_baselines(
    db: AsyncSession,
    *,
    season_range: str = DEFAULT_SEASON_RANGE,
    min_minutes: float = DEFAULT_MIN_MINUTES,
) -> str:
    """Build a new versioned T1 baseline set from SL history and activate it.

    Reads every ``SummerLeaguePlayerSeason`` row within ``season_range``,
    blends each player's same-year rows into an event-aggregate GmSc (the
    min-minutes gate applied here), assigns each event to its player's
    slot/round/status cohort (event grain) and — for each player's earliest
    qualifying year — their debut cohort (debut grain), computes
    breakpoints/mean/median per cohort, and writes them all under one new
    ``baseline_version`` with ``is_active=True``. Every row from every prior
    version is flipped to ``is_active=False`` in the same call — old rows are
    never deleted or mutated otherwise, so a rebuild is always idempotent and
    non-destructive.

    Does not commit; the caller controls the transaction (mirrors
    ``app.services.summer_league.metrics.rebuild``).

    Args:
        db: Async session.
        season_range: ``"<start>-<end>"`` inclusive year bounds, e.g.
            ``"2017-2025"``.
        min_minutes: Minimum blended minutes for a player-year event to enter
            the distribution.

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

    player_ids = {pid for pid, _year in events}
    slot_stmt = select(  # type: ignore[call-overload]
        PlayerMaster.id, PlayerMaster.draft_round, PlayerMaster.draft_pick
    ).where(
        PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
    )
    draft_slot: dict[int, tuple[Optional[int], Optional[int]]] = {
        pid: (rnd, pick) for pid, rnd, pick in (await db.execute(slot_stmt)).all()
    }

    debut_year: dict[int, int] = {}
    for pid, year in events:
        if pid not in debut_year or year < debut_year[pid]:
            debut_year[pid] = year

    event_values: dict[str, list[float]] = defaultdict(list)
    event_meta: dict[
        str, tuple[Optional[tuple[int, int]], SummerLeagueDeskCohortKind]
    ] = {}
    debut_values: dict[str, list[float]] = defaultdict(list)
    debut_meta: dict[str, Optional[tuple[int, int]]] = {}

    for (pid, year), agg in events.items():
        rnd, pick = draft_slot.get(pid, (None, None))
        suffix, bounds, kind = _bucket(rnd, pick)

        event_key = cohort_key_for(rnd, pick, grain=SummerLeagueDeskGrain.EVENT)
        event_values[event_key].append(agg.gmsc)
        event_meta[event_key] = (bounds, kind)

        if debut_year[pid] == year:
            debut_key = f"debut:{suffix}"
            debut_values[debut_key].append(agg.gmsc)
            debut_meta[debut_key] = bounds

    version = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")

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
                min_minutes=min_minutes,
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

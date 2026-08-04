"""Job B step 2 — the Summer League Desk cohort percentile + grade service (T2).

Ranks one player's Summer League **event-aggregate GmSc** (blended across every
venue they've played this year, same grain #502's Job A used to build T1)
against the **active** cohort baseline for their draft-slot/status cohort, and
upserts the result into ``summer_league_desk_player_grades`` (T2)
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §6, §10 T2, Job B
step 2).

**Reuses, doesn't re-derive:**

* Cohort classification (``cohort_key_for``) and the event-aggregate blend
  (``blend_event_aggregates``) from ``app.services.sources.summer_league.cohort_baselines``
  (#502) — the ``draft_pick`` is WITHIN-ROUND gotcha lives there once.
* The **adaptive gate ladder** (2 games/60 min -> 1/20 -> 1/0) shipped for the
  Leaders board (``app.services.summer_league_leaders_service.GATE_LADDER``),
  applied here to a single subject's own event sample rather than a whole
  leaderboard population, plus that same module's ``TARGET_BOARD_ROWS`` (10)
  as the floor for a cohort baseline being too thin to trust at all.
* ``SummerLeagueDerivedAgg.gmsc`` — already computed from
  ``game_score_line()`` (`app/services/summer_league/metrics.py`) when the
  materialized per-(player, competition) row is built; grading reads that
  column via the same blend path #502 uses rather than recomputing GmSc from
  raw box logs.

T1 is never rebuilt here — a ``ValueError`` when no active baseline row
exists for the player's cohort is a real error (Job A hasn't run / the
cohort has no history), not a fallback to compute one on the fly.
"""

from __future__ import annotations

# discipline: file-size version-aware Desk read compatibility; no new service surface

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskGrade,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
)
from app.schemas.summer_league_metrics import SummerLeagueDerivedAgg
from app.services.stats.percentiles import percentile_of
from app.services.sources.summer_league.cohort_baselines import (
    blend_event_aggregates,
    cohort_key_for,
)
from app.services.summer_league_leaders_service import GATE_LADDER, TARGET_BOARD_ROWS

logger = logging.getLogger(__name__)

# Grade thresholds (#503 Definition of Done, behavior spec §6/§10 T2).
# Checked highest-first; the first floor a percentile clears wins.
GRADE_THRESHOLDS: tuple[tuple[float, SummerLeagueDeskGrade], ...] = (
    (90.0, SummerLeagueDeskGrade.HOT),
    (65.0, SummerLeagueDeskGrade.WARM),
    (40.0, SummerLeagueDeskGrade.MID),
)


@dataclass(frozen=True)
class GradeRow:
    """The graded outcome for one (player, competition, baseline_version).

    A plain DTO mirroring the T2 row shape, decoupled from the ORM/session
    lifecycle so downstream consumers (#504 storyline engine, #520 Class
    Tracker read service) don't need a live, unexpired SQLModel instance.
    """

    player_id: int
    competition_id: int
    baseline_version: str
    cohort_key: str
    subject_value: float
    pctl: float
    grade: SummerLeagueDeskGrade
    n_cohort: int
    gated: bool


def is_gated(rung: Optional[int], n_members: int) -> bool:
    """Whether a graded outcome should be flagged as not-yet-confident.

    Split out from :func:`grade_player_event` as a small pure predicate so
    the gate decision is unit-testable without a database: gated whenever
    the subject's own sample only cleared a relaxed :data:`GATE_LADDER`
    rung (or none at all), OR the T1 cohort baseline itself is too thin
    (fewer than :data:`~app.services.summer_league_leaders_service.TARGET_BOARD_ROWS`
    historical members) to trust regardless of the subject's sample.

    Args:
        rung: Result of :func:`gate_rung` for the subject's (gp, minutes).
        n_members: The T1 baseline row's ``n_members`` (cohort sample size).

    Returns:
        ``True`` when the percentile should be treated as not confident.
    """
    return rung is None or rung > 0 or n_members < TARGET_BOARD_ROWS


def grade_for_percentile(pctl: float) -> SummerLeagueDeskGrade:
    """Map a percentile (0-100) to its coarse grade bucket.

    Args:
        pctl: A cohort percentile, 0-100.

    Returns:
        ``hot`` (>=90) / ``warm`` (65-89) / ``mid`` (40-64) / ``cold`` (<40).
    """
    for floor, grade in GRADE_THRESHOLDS:
        if pctl >= floor:
            return grade
    return SummerLeagueDeskGrade.COLD


def percentile_of_value(breakpoints: dict[str, float], value: float) -> float:
    """Invert a T1 ``breakpoints`` map (percentile -> value) to rank ``value``.

    ``compute_breakpoints`` (#502) fits a monotonically non-decreasing
    percentile -> value grid via linear interpolation over a sorted
    distribution. This is the reverse lookup: given a subject's value, find
    the percentile it falls at by locating the bracketing grid points and
    linearly interpolating back, mirroring numpy's ``'linear'`` method so a
    value at exactly a fitted breakpoint returns exactly that percentile.

    Args:
        breakpoints: ``{"0": ..., "5": ..., ..., "100": ...}`` from a T1 row.
        value: The subject's metric value (event-aggregate GmSc) to rank.

    Returns:
        The interpolated percentile, 0-100 (clamped at the grid's ends for
        values outside the observed range), rounded to 2 decimals.

    Raises:
        ValueError: ``breakpoints`` is empty (an empty/never-built cohort).
    """
    if not breakpoints:
        raise ValueError("Cannot rank a value against an empty breakpoints map.")
    return round(percentile_of(breakpoints, value), 2)


def gate_rung(gp: int, minutes: float) -> Optional[int]:
    """The index of the strictest :data:`GATE_LADDER` rung ``(gp, minutes)`` clears.

    Reuses the exact ladder the Leaders board walks (2 games/60 minutes ->
    1/20 -> 1/0), applied here to a single subject's own event sample instead
    of a whole leaderboard population. Rung 0 (the standard gate) means a
    confident sample; any later rung means the sample only cleared a relaxed
    rung — i.e. still thin.

    Args:
        gp: The subject's blended games played for the event.
        minutes: The subject's blended minutes for the event.

    Returns:
        The 0-based rung index, or ``None`` if the sample doesn't even clear
        the loosest rung (no games at all — nothing to grade).
    """
    for i, (min_gp, min_minutes) in enumerate(GATE_LADDER):
        if gp >= min_gp and minutes >= min_minutes:
            return i
    return None


def _grade_row_from_baseline(
    *,
    player_id: int,
    competition_id: int,
    baseline_version: str,
    cohort_key: str,
    agg_gmsc: float,
    agg_gp: int,
    agg_minutes: float,
    baseline: SummerLeagueCohortBaseline,
) -> GradeRow:
    """Pure percentile/grade/gate core shared by the single- and bulk-grade paths.

    Split out (#548) so :func:`grade_player_event` (one player, its own
    per-call fetches) and :func:`grade_players_bulk` (every player in one
    tick, batched fetches) always compute an identical outcome for the same
    inputs -- there is exactly one place this math lives.

    Args:
        player_id: The player's ID in ``players_master``.
        competition_id: The competition this grade is being written under.
        baseline_version: Which T1 ``baseline_version`` this ranks against.
        cohort_key: The player's event-grain cohort key (already classified).
        agg_gmsc: The player's blended event-aggregate GmSc.
        agg_gp: The player's blended event-aggregate games played.
        agg_minutes: The player's blended event-aggregate minutes.
        baseline: The active T1 baseline row for ``cohort_key``.

    Returns:
        The graded outcome as a :class:`GradeRow`.
    """
    pctl = percentile_of_value(baseline.breakpoints, agg_gmsc)
    grade = grade_for_percentile(pctl)
    rung = gate_rung(agg_gp, agg_minutes)
    gated = is_gated(rung, baseline.n_members)
    return GradeRow(
        player_id=player_id,
        competition_id=competition_id,
        baseline_version=baseline_version,
        cohort_key=cohort_key,
        subject_value=agg_gmsc,
        pctl=pctl,
        grade=grade,
        n_cohort=baseline.n_members,
        gated=gated,
    )


async def grade_player_event(
    session: AsyncSession,
    player_id: int,
    competition_id: int,
    *,
    baseline_version: str,
) -> GradeRow:
    """Rank a player's SL event-aggregate GmSc against their cohort baseline (T1).

    Reads every ``SummerLeagueDerivedAgg`` row for ``player_id`` in
    ``competition_id``'s year (across every venue they played, same
    event-aggregate grain #502's Job A used to build T1), blends them
    games-weighted with no eligibility floor (``min_minutes=0.0`` — unlike
    Job A's historical build, a live tick must still grade a thin/1-game
    subject; the gate ladder below is what flags that thinness rather than
    the blend silently dropping the player), classifies the player's cohort
    via #502's ``cohort_key_for``, ranks the blended value against the
    active T1 baseline row for that cohort, applies the adaptive gate ladder,
    and upserts the outcome into T2
    (``player_id``, ``competition_id``, ``baseline_version`` unique).

    Does not commit; the caller controls the transaction (mirrors
    ``app.services.sources.summer_league.cohort_baselines.build_baselines``).

    Args:
        session: Active database session.
        player_id: The player's ID in ``players_master``.
        competition_id: The ``summer_league_competitions`` row identifying
            which (year, venue) tick context this grade is being written
            under. The subject value still blends across every venue the
            player played that year, not just this competition's venue.
        baseline_version: Which T1 ``baseline_version`` to rank against
            (must have an active row for the player's cohort).

    Returns:
        The graded outcome as a :class:`GradeRow`.

    Raises:
        ValueError: ``competition_id``/``player_id`` don't resolve, the
            player has no Summer League game data for the competition's
            year, or no active T1 baseline row exists for the player's
            cohort under ``baseline_version``.
    """
    competition = await session.get(SummerLeagueEdition, competition_id)
    if competition is None:
        raise ValueError(f"No summer_league_competitions row for id={competition_id}.")

    player = await session.get(PlayerMaster, player_id)
    if player is None:
        raise ValueError(f"No players_master row for id={player_id}.")

    season_stmt = select(  # type: ignore[call-overload]
        SummerLeagueDerivedAgg.player_id,
        SummerLeagueDerivedAgg.year,
        SummerLeagueDerivedAgg.gmsc,
        SummerLeagueDerivedAgg.minutes,
        SummerLeagueDerivedAgg.gp,
    ).where(
        SummerLeagueDerivedAgg.player_id == player_id,
        SummerLeagueDerivedAgg.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeagueDerivedAgg.year == competition.year,
    )
    rows = (await session.execute(season_stmt)).all()

    events = blend_event_aggregates(rows, min_minutes=0.0)
    agg = events.get((player_id, competition.year))
    if agg is None:
        raise ValueError(
            f"No Summer League game data for player_id={player_id} in "
            f"{competition.year} (competition_id={competition_id})."
        )

    cohort_key = cohort_key_for(
        player.draft_round, player.draft_pick, grain=SummerLeagueDeskGrain.EVENT
    )

    baseline_stmt = select(SummerLeagueCohortBaseline).where(
        SummerLeagueCohortBaseline.baseline_version == baseline_version,  # type: ignore[arg-type]
        SummerLeagueCohortBaseline.cohort_key == cohort_key,  # type: ignore[arg-type]
        SummerLeagueCohortBaseline.is_active.is_(True),  # type: ignore[attr-defined]
    )
    baseline = (await session.execute(baseline_stmt)).scalar_one_or_none()
    if baseline is None:
        raise ValueError(
            f"No active T1 baseline for cohort_key={cohort_key!r} under "
            f"baseline_version={baseline_version!r}."
        )

    grade_row = _grade_row_from_baseline(
        player_id=player_id,
        competition_id=competition_id,
        baseline_version=baseline_version,
        cohort_key=cohort_key,
        agg_gmsc=agg.gmsc,
        agg_gp=agg.gp,
        agg_minutes=agg.minutes,
        baseline=baseline,
    )

    values = {
        "player_id": player_id,
        "competition_id": competition_id,
        "baseline_version": baseline_version,
        "cohort_key": cohort_key,
        "subject_value": grade_row.subject_value,
        "pctl": grade_row.pctl,
        "grade": grade_row.grade,
        "n_cohort": grade_row.n_cohort,
        "gated": grade_row.gated,
        "computed_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    stmt = insert(SummerLeagueDeskPlayerGrade).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_summer_league_desk_player_grades_player_competition_version",
        set_=values,
    )
    await session.execute(stmt)
    await session.flush()

    return grade_row


async def grade_players_bulk(
    session: AsyncSession,
    player_ids: Sequence[int],
    competition_id: int,
    *,
    baseline_version: str,
) -> dict[int, GradeRow]:
    """Grade every player in ``player_ids`` against the active T1 baseline in O(1) queries.

    Job B step 2's bulk entry point (#548) -- replaces a per-player loop over
    :func:`grade_player_event` (which issued a competition fetch, a player
    fetch, a season-rows fetch, a baseline fetch, and an upsert PER PLAYER --
    5 queries x roster size) with exactly 4 batched queries total regardless
    of roster size: one competition fetch, one ``players_master`` fetch, one
    ``SummerLeagueDerivedAgg`` fetch, one baseline fetch over every distinct
    cohort key this roster spans, plus ONE multi-row upsert statement (a
    single ``INSERT ... VALUES (...), (...), ... ON CONFLICT DO UPDATE`` --
    still one round trip no matter how many players qualify).

    Reuses the exact same math :func:`grade_player_event` does
    (:func:`_grade_row_from_baseline`), so a bulk-graded and a single-graded
    player under identical inputs always produce byte-identical
    :class:`GradeRow` output -- this is what the parity contract between the
    two paths rests on, not a re-derivation.

    A player is silently omitted from the result (never raises) for any of
    the same reasons :func:`grade_player_event` raises for a single player --
    no ``players_master`` row, no Summer League game data this competition's
    year, or no active T1 baseline for their cohort -- mirroring the
    try/except-per-player skip `app/cli/sl_desk_tick.py` used to perform
    around the old per-player loop, just logged in aggregate here since a
    batched fetch has no natural per-player failure point to catch.

    Does not commit; the caller controls the transaction (matches
    :func:`grade_player_event`).

    Args:
        session: Active database session.
        player_ids: Every player to grade (duplicates tolerated; deduped
            internally). Empty returns ``{}`` with no queries at all.
        competition_id: The ``summer_league_competitions`` row identifying
            which (year, venue) tick context these grades are being written
            under -- same semantics as :func:`grade_player_event`.
        baseline_version: Which T1 ``baseline_version`` to rank against.

    Returns:
        ``player_id -> GradeRow`` for every player that graded successfully.
        Missing keys are the silently-skipped players (logged at info level).

    Raises:
        ValueError: ``competition_id`` doesn't resolve to a
            ``summer_league_competitions`` row.
    """
    if not player_ids:
        return {}

    competition = await session.get(SummerLeagueEdition, competition_id)
    if competition is None:
        raise ValueError(f"No summer_league_competitions row for id={competition_id}.")

    unique_ids = list(dict.fromkeys(player_ids))

    players_stmt = select(PlayerMaster).where(  # type: ignore[call-overload]
        PlayerMaster.id.in_(unique_ids)  # type: ignore[union-attr]
    )
    players = (await session.execute(players_stmt)).scalars().all()
    player_by_id = {p.id: p for p in players if p.id is not None}

    season_stmt = select(  # type: ignore[call-overload]
        SummerLeagueDerivedAgg.player_id,
        SummerLeagueDerivedAgg.year,
        SummerLeagueDerivedAgg.gmsc,
        SummerLeagueDerivedAgg.minutes,
        SummerLeagueDerivedAgg.gp,
    ).where(
        SummerLeagueDerivedAgg.player_id.in_(unique_ids),  # type: ignore[attr-defined]
        SummerLeagueDerivedAgg.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeagueDerivedAgg.year == competition.year,
    )
    season_rows = (await session.execute(season_stmt)).all()
    events = blend_event_aggregates(season_rows, min_minutes=0.0)

    cohort_key_by_player: dict[int, str] = {}
    for player_id in unique_ids:
        player = player_by_id.get(player_id)
        if player is None:
            logger.info(
                "grade_players_bulk: skip player_id=%s (no players_master row).",
                player_id,
            )
            continue
        if (player_id, competition.year) not in events:
            logger.info(
                "grade_players_bulk: skip player_id=%s competition_id=%s "
                "(no Summer League game data for %s).",
                player_id,
                competition_id,
                competition.year,
            )
            continue
        cohort_key_by_player[player_id] = cohort_key_for(
            player.draft_round, player.draft_pick, grain=SummerLeagueDeskGrain.EVENT
        )

    cohort_keys = sorted(set(cohort_key_by_player.values()))
    baseline_by_cohort: dict[str, SummerLeagueCohortBaseline] = {}
    if cohort_keys:
        baseline_stmt = select(SummerLeagueCohortBaseline).where(
            SummerLeagueCohortBaseline.baseline_version == baseline_version,  # type: ignore[arg-type]
            SummerLeagueCohortBaseline.cohort_key.in_(cohort_keys),  # type: ignore[attr-defined]
            SummerLeagueCohortBaseline.is_active.is_(True),  # type: ignore[attr-defined]
        )
        baseline_rows = (await session.execute(baseline_stmt)).scalars().all()
        baseline_by_cohort = {row.cohort_key: row for row in baseline_rows}

    computed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    graded: dict[int, GradeRow] = {}
    value_rows: list[dict[str, object]] = []
    for player_id, cohort_key in cohort_key_by_player.items():
        baseline = baseline_by_cohort.get(cohort_key)
        if baseline is None:
            logger.info(
                "grade_players_bulk: skip player_id=%s (no active T1 baseline "
                "for cohort_key=%r under baseline_version=%r).",
                player_id,
                cohort_key,
                baseline_version,
            )
            continue
        agg = events[(player_id, competition.year)]
        grade_row = _grade_row_from_baseline(
            player_id=player_id,
            competition_id=competition_id,
            baseline_version=baseline_version,
            cohort_key=cohort_key,
            agg_gmsc=agg.gmsc,
            agg_gp=agg.gp,
            agg_minutes=agg.minutes,
            baseline=baseline,
        )
        graded[player_id] = grade_row
        value_rows.append(
            {
                "player_id": player_id,
                "competition_id": competition_id,
                "baseline_version": baseline_version,
                "cohort_key": cohort_key,
                "subject_value": grade_row.subject_value,
                "pctl": grade_row.pctl,
                "grade": grade_row.grade,
                "n_cohort": grade_row.n_cohort,
                "gated": grade_row.gated,
                "computed_at": computed_at,
            }
        )

    if value_rows:
        # ONE multi-row INSERT ... VALUES (...), (...), ... ON CONFLICT DO
        # UPDATE -- a single statement/round trip regardless of how many
        # players graded this tick (never a per-row `execute`/`flush`).
        bulk_stmt = insert(SummerLeagueDeskPlayerGrade).values(value_rows)
        bulk_stmt = bulk_stmt.on_conflict_do_update(
            constraint="uq_summer_league_desk_player_grades_player_competition_version",
            set_={
                "cohort_key": bulk_stmt.excluded.cohort_key,
                "subject_value": bulk_stmt.excluded.subject_value,
                "pctl": bulk_stmt.excluded.pctl,
                "grade": bulk_stmt.excluded.grade,
                "n_cohort": bulk_stmt.excluded.n_cohort,
                "gated": bulk_stmt.excluded.gated,
                "computed_at": bulk_stmt.excluded.computed_at,
            },
        )
        await session.execute(bulk_stmt)
        await session.flush()

    return graded

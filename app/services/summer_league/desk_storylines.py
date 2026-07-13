"""The Summer League Desk storyline engine (#504).

Five deterministic triggers over tonight's tracked prospects, a
``weight = base[type] x magnitude[instance]`` scoring rule, a pure slate
ranker (Morning vs Live), and hero selection with a quiet-slate fallback
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §3, §4, §5, §10 T3/T4).

**No Stakes / no competitive framing.** SL wins/losses and standings are never
a storyline; a game's score is only ever context for locating a player's line.
Nothing in this module emits or implies tournament, elimination, or bracket
language. **Status heat** (formerly "Contract watch") is a percentile-tracking
trigger only -- this module holds no two-way/Exhibit-10/signing/contract data
and never implies any.

**Streak trigger data source (#525):** the ``streak`` trigger needs a per-game
"cohort-median GmSc" bar and a per-game percentile. #502's Job A originally
shipped only **event**- and **debut**-grain T1 baselines, so this module used
to rank each individual game against the event-aggregate distribution -- a
documented approximation (event aggregates have much lower variance than
individual games, which stretches percentiles toward the tails). #525 added a
``game``-grain T1 baseline (`cohort_baselines.build_baselines`, pooling every
qualifying individual-game GmSc per cohort) and retargeted this module to it:
the bar is the game-grain row's ``median_value``, and the per-game percentile
is ``desk_grades.percentile_of_value(game_row.breakpoints, game_gmsc)``. #520's
``detect_streak`` was built with exactly this caller-supplied-``pctl`` seam
for this reason; this module is that caller.

Two layers:

* **Pure, unit-testable core** -- ``base_weight``, the five ``detect_*``
  trigger functions, ``rank_slate``, and ``select_quiet_slate_hero`` all take
  plain dataclasses, never touch a session, and are deterministic.
* **Async orchestration** -- ``compute_desk_storylines`` fetches the plain
  inputs the pure core needs (tonight's rosters, consensus rank, T1/T2 rows,
  prior-year events, this competition's game log) and upserts T3
  (``summer_league_desk_storylines``) + T4 (``summer_league_desk_slate``).
  Both tables are rebuildable projections (module docstring,
  `app/schemas/summer_league_desk.py`); each call fully replaces the T3 rows
  for the games it touches rather than accumulating duplicates across ticks.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal, Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
    SummerLeagueDeskStoryline,
    SummerLeagueDeskTriggerType,
)
from app.schemas.player_affiliation import AffiliationStatus
from app.services.summer_league.cohort_baselines import (
    FirstQualifyingGame,
    cohort_key_for,
)
from app.services.summer_league.desk_facts import (
    FactSubject,
    GameLine,
    PriorEvent,
    detect_self_delta,
)
from app.services.summer_league.desk_facts import detect_streak as _facts_detect_streak
from app.services.summer_league.desk_fact_queries import (
    fetch_current_event_gp,
    fetch_first_qualifying_games,
    fetch_game_lines,
    fetch_prior_events,
)
from app.services.summer_league.desk_grades import GradeRow, percentile_of_value
from app.services.summer_league.metrics import game_score_from_row

# --------------------------------------------------------------------------- #
# Weighting priors (behavior spec §3, Pinned decision #1)
# --------------------------------------------------------------------------- #
BASE_WEIGHTS: dict[SummerLeagueDeskTriggerType, float] = {
    SummerLeagueDeskTriggerType.DUEL: 90.0,
    SummerLeagueDeskTriggerType.DEBUT: 80.0,
    SummerLeagueDeskTriggerType.SECOND_LOOK: 70.0,
    SummerLeagueDeskTriggerType.STREAK: 65.0,
    SummerLeagueDeskTriggerType.STATUS_HEAT: 60.0,
}


def base_weight(trigger: str) -> float:
    """The intrinsic (kind-level) headline-worthiness prior for a trigger type.

    Args:
        trigger: One of the five ``SummerLeagueDeskTriggerType`` values
            (``"debut"``/``"duel"``/``"streak"``/``"status_heat"``/
            ``"second_look"``), as a string or the enum member itself.

    Returns:
        The base weight prior (0-100).

    Raises:
        ValueError: ``trigger`` isn't one of the five known trigger types.
    """
    try:
        key = SummerLeagueDeskTriggerType(trigger)
    except ValueError as exc:
        raise ValueError(f"Unknown storyline trigger_type: {trigger!r}") from exc
    return BASE_WEIGHTS[key]


# --------------------------------------------------------------------------- #
# Prominence (magnitude driver for debut/duel; consensus rank, fallback slot)
# --------------------------------------------------------------------------- #
DUEL_PROMINENCE_CUTOFF = 14  # Pinned decision #2.
_PROMINENCE_UNRANKED_FLOOR = 10.0
_PROMINENCE_MAX_RANK_SPAN = 60.0  # picks 1-60; rank beyond this floors to 0.


def draft_slot_fallback(
    draft_round: Optional[int], draft_pick: Optional[int]
) -> Optional[int]:
    """The overall draft-position fallback used when no consensus rank exists.

    ``players_master.draft_pick`` is WITHIN-ROUND (repo-wide gotcha) -- round 1
    picks are already overall (1-30); round 2 picks need +30 to become overall
    (31-60). Undrafted (``draft_round is None``) has no fallback slot.

    Args:
        draft_round: ``players_master.draft_round``.
        draft_pick: ``players_master.draft_pick`` (within-round).

    Returns:
        The overall draft position, or ``None`` when undrafted/unknown.
    """
    if draft_pick is None or draft_round is None:
        return None
    if draft_round == 1:
        return draft_pick
    if draft_round == 2:
        return 30 + draft_pick
    return None


def effective_prominence_rank(
    consensus_rank: Optional[int],
    draft_round: Optional[int],
    draft_pick: Optional[int],
) -> Optional[int]:
    """Consensus rank, falling back to draft slot, per behavior spec §3.

    Args:
        consensus_rank: The player's most recent DraftGuru consensus rank,
            or ``None`` if they've never appeared on a snapshot.
        draft_round: ``players_master.draft_round``.
        draft_pick: ``players_master.draft_pick`` (within-round).

    Returns:
        The rank to use for prominence, or ``None`` when neither source has
        one (undrafted and never boarded).
    """
    if consensus_rank is not None:
        return consensus_rank
    return draft_slot_fallback(draft_round, draft_pick)


def prominence_score(rank: Optional[int]) -> float:
    """A 0-100 prominence score from an effective rank -- #1 scores highest.

    Linearly decays from 100 at rank 1 to 0 at rank 61+ (the roughly one
    draft's worth of "prominent" players this desk covers). A player with no
    rank at all (undrafted, unranked) still gets a small floor score rather
    than a hard zero, since an unranked player can still be a debut/duel
    subject -- just not a prominent one.

    Args:
        rank: An effective prominence rank (see
            :func:`effective_prominence_rank`), or ``None``.

    Returns:
        A score in ``[0, 100]``.
    """
    if rank is None:
        return _PROMINENCE_UNRANKED_FLOOR
    rank = max(1, rank)
    return round(max(0.0, 100.0 - (rank - 1) * (100.0 / _PROMINENCE_MAX_RANK_SPAN)), 2)


# --------------------------------------------------------------------------- #
# Trigger instance + input shapes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TriggerInstance:
    """One fired trigger -- mirrors a T3 (``summer_league_desk_storylines``) row."""

    trigger_type: SummerLeagueDeskTriggerType
    subject_player_id: int
    subject_player_id_2: Optional[int]
    base_weight: float
    magnitude: float
    weight: float
    # Null pre-tip; filled by the live re-ranking pass (Morning -> Live).
    realized_deviation: Optional[float] = None


@dataclass(frozen=True)
class ProspectSlot:
    """A tracked player's context for one game night -- the detectors' input unit."""

    player_id: int
    player_label: str
    draft_round: Optional[int]
    draft_pick: Optional[int]
    consensus_rank: Optional[int] = None


def _weight(base: float, magnitude: float) -> float:
    return round(base * magnitude, 2)


# --------------------------------------------------------------------------- #
# Trigger 1 -- Debut
# --------------------------------------------------------------------------- #
def detect_debut(*, subject: ProspectSlot, is_debut: bool) -> Optional[TriggerInstance]:
    """Debut trigger: the subject has no prior Summer League game log.

    Args:
        subject: The tracked player.
        is_debut: Whether this is genuinely the subject's first-ever SL game
            (caller-determined from game-log history -- this detector doesn't
            look anything up, per the pure-detector pattern).

    Returns:
        ``None`` when ``is_debut`` is false.
    """
    if not is_debut:
        return None
    rank = effective_prominence_rank(
        subject.consensus_rank, subject.draft_round, subject.draft_pick
    )
    magnitude = prominence_score(rank)
    base = base_weight(SummerLeagueDeskTriggerType.DEBUT.value)
    return TriggerInstance(
        trigger_type=SummerLeagueDeskTriggerType.DEBUT,
        subject_player_id=subject.player_id,
        subject_player_id_2=None,
        base_weight=base,
        magnitude=magnitude,
        weight=_weight(base, magnitude),
    )


# --------------------------------------------------------------------------- #
# Trigger 2 -- Duel
# --------------------------------------------------------------------------- #
def detect_duel(*, candidates: Sequence[ProspectSlot]) -> Optional[TriggerInstance]:
    """Duel trigger: two prominent prospects (consensus/slot <=14) share a game.

    When more than two candidates qualify, the two most prominent form the
    duel (a game is never split into more than one duel storyline).

    Args:
        candidates: Every tracked player in the game (both teams).

    Returns:
        ``None`` when fewer than two candidates clear
        :data:`DUEL_PROMINENCE_CUTOFF`.
    """
    qualifying: list[tuple[int, ProspectSlot]] = []
    for c in candidates:
        rank = effective_prominence_rank(c.consensus_rank, c.draft_round, c.draft_pick)
        if rank is not None and rank <= DUEL_PROMINENCE_CUTOFF:
            qualifying.append((rank, c))
    if len(qualifying) < 2:
        return None

    qualifying.sort(key=lambda t: t[0])
    (rank_a, a), (rank_b, b) = qualifying[0], qualifying[1]
    magnitude = round((prominence_score(rank_a) + prominence_score(rank_b)) / 2.0, 2)
    base = base_weight(SummerLeagueDeskTriggerType.DUEL.value)
    return TriggerInstance(
        trigger_type=SummerLeagueDeskTriggerType.DUEL,
        subject_player_id=a.player_id,
        subject_player_id_2=b.player_id,
        base_weight=base,
        magnitude=magnitude,
        weight=_weight(base, magnitude),
    )


# --------------------------------------------------------------------------- #
# Trigger 3 -- Streak (game-grain baseline; see module docstring)
# --------------------------------------------------------------------------- #
def detect_streak(
    *,
    subject: ProspectSlot,
    cohort_key: Optional[str],
    games: Sequence[GameLine],
) -> Optional[TriggerInstance]:
    """Streak trigger: >=3 straight games at/above cohort median, avg pctl >=65.

    Delegates the actual run-detection to #520's
    ``desk_facts.detect_streak`` (Pinned decision #3) rather than
    reimplementing it -- this function only adapts the resulting
    :class:`~app.services.summer_league.desk_facts.Fact` into a
    :class:`TriggerInstance`. ``games`` must already carry each line's
    game-grain ``pctl`` (see module docstring); a line with ``pctl=None``
    stops the run exactly as ``desk_facts.detect_streak`` documents.

    Args:
        subject: The tracked player.
        cohort_key: The cohort the median/percentiles are scoped to
            (**game-grain**, e.g. ``game:1-4`` -- see module docstring).
        games: The player's SL game log this competition, oldest first,
            *not including* tonight's game (an "entering" run).

    Returns:
        ``None`` when no qualifying run exists.
    """
    fact = _facts_detect_streak(
        subject=FactSubject(
            player_id=subject.player_id, player_label=subject.player_label
        ),
        metric="gmsc",
        cohort_key=cohort_key,
        games=games,
    )
    if fact is None:
        return None

    magnitude = round(fact.notability * 100.0, 2)
    base = base_weight(SummerLeagueDeskTriggerType.STREAK.value)
    return TriggerInstance(
        trigger_type=SummerLeagueDeskTriggerType.STREAK,
        subject_player_id=subject.player_id,
        subject_player_id_2=None,
        base_weight=base,
        magnitude=magnitude,
        weight=_weight(base, magnitude),
    )


# --------------------------------------------------------------------------- #
# Trigger 4 -- Status heat (renamed from "Contract watch" -- no contract data)
# --------------------------------------------------------------------------- #
STATUS_HEAT_PCTL_FLOOR = 85.0


def _status_heat_eligible(draft_round: Optional[int]) -> bool:
    """Undrafted (``draft_round is None``) or 2nd-round only."""
    return draft_round is None or draft_round == 2


def detect_status_heat(
    *, subject: ProspectSlot, grade: Optional[GradeRow]
) -> Optional[TriggerInstance]:
    """Status heat trigger: undrafted/2nd-round player >= 85th pctl vs status cohort.

    Copy/identifiers here never imply two-way, Exhibit-10, signing, or roster
    decisions -- this fires purely off the T2 cohort percentile (#503). A
    :attr:`GradeRow.gated` percentile is not confident enough to cross a hard
    85th-percentile bar, so a gated grade **never** fires this trigger (per
    ticket instruction: "a gated grade should not drive a confident
    storyline") -- unlike ``desk_facts.detect_percentile``, which merely
    dampens notability, this trigger declines outright.

    Args:
        subject: The tracked player.
        grade: The player's current T2 grade, or ``None`` if ungraded.

    Returns:
        ``None`` when ineligible, ungraded, gated, or below the pctl floor.
    """
    if grade is None or grade.gated:
        return None
    if not _status_heat_eligible(subject.draft_round):
        return None
    if grade.pctl < STATUS_HEAT_PCTL_FLOOR:
        return None

    magnitude = round(grade.pctl, 2)
    base = base_weight(SummerLeagueDeskTriggerType.STATUS_HEAT.value)
    return TriggerInstance(
        trigger_type=SummerLeagueDeskTriggerType.STATUS_HEAT,
        subject_player_id=subject.player_id,
        subject_player_id_2=None,
        base_weight=base,
        magnitude=magnitude,
        weight=_weight(base, magnitude),
        realized_deviation=round(grade.pctl - 50.0, 2),
    )


# --------------------------------------------------------------------------- #
# Trigger 5 -- 2nd look
# --------------------------------------------------------------------------- #
def detect_second_look(
    *,
    subject: ProspectSlot,
    current_value: float,
    current_gp: int,
    prior: Optional[PriorEvent],
    current_pctl: Optional[float] = None,
    prior_pctl: Optional[float] = None,
    gated: bool = False,
) -> Optional[TriggerInstance]:
    """2nd look trigger: a returner tracking meaningfully vs his own prior SL.

    Delegates the firing decision to #520's ``desk_facts.detect_self_delta``
    (a returner with no notable GmSc swing, or a debutant with no ``prior``,
    doesn't fire) rather than reimplementing the threshold. When both
    ``current_pctl`` and ``prior_pctl`` are available (both ranked against
    the same cohort baseline), magnitude is the pctl swing (the spec's
    "pctl deviation vs his own prior SL" driver); otherwise it falls back to
    a GmSc-delta-scaled magnitude on a comparable 0-100 scale. A gated
    current-event grade never fires this trigger -- a thin sample shouldn't
    drive a confident "meaningfully above/below" claim.

    Args:
        subject: The tracked player.
        current_value: This event's GmSc.
        current_gp: This event's games played.
        prior: The subject's most recent prior-year SL event, or ``None``.
        current_pctl: The current event's cohort percentile, if known.
        prior_pctl: The prior event's cohort percentile (ranked against the
            *same* baseline), if known.
        gated: Whether the subject's current T2 grade is gated.

    Returns:
        ``None`` when gated, or ``desk_facts.detect_self_delta`` declines.
    """
    if gated:
        return None

    fact = detect_self_delta(
        subject=FactSubject(
            player_id=subject.player_id, player_label=subject.player_label
        ),
        metric="gmsc",
        cohort_key=None,
        current_value=current_value,
        current_gp=current_gp,
        prior=prior,
    )
    if fact is None:
        return None

    if current_pctl is not None and prior_pctl is not None:
        magnitude = round(abs(current_pctl - prior_pctl), 2)
    else:
        delta = abs(float(fact.values["delta"]))
        # Scale so a delta at the notability floor (~3 GmSc) reads as a
        # moderate (~50) magnitude on the same rough 0-100 scale as the
        # pctl-based path, capped at 100.
        magnitude = round(min(100.0, delta * (50.0 / 3.0)), 2)

    base = base_weight(SummerLeagueDeskTriggerType.SECOND_LOOK.value)
    return TriggerInstance(
        trigger_type=SummerLeagueDeskTriggerType.SECOND_LOOK,
        subject_player_id=subject.player_id,
        subject_player_id_2=None,
        base_weight=base,
        magnitude=magnitude,
        weight=_weight(base, magnitude),
    )


# --------------------------------------------------------------------------- #
# Realized (game-grain) deviation -- #541's Live re-rank score
# --------------------------------------------------------------------------- #
def realized_deviation_from_pctl(pctl: float) -> float:
    """Absolute percentile distance from 50 -- the Live re-rank score for one line.

    A 90th-percentile line and a 10th-percentile line both score 40: Live
    ordering cares about how far a realized performance sits from the
    cohort's midpoint in EITHER direction (a historically rough outing is
    just as much a live storyline as a historically great one), not which
    side of the median it fell on.

    Args:
        pctl: A game-grain cohort percentile (0-100), e.g. from
            ``desk_grades.percentile_of_value`` against a ``game``-grain T1
            baseline (#525).

    Returns:
        ``abs(pctl - 50.0)``, rounded to 2 decimals -- always in ``[0, 50]``.
    """
    return round(abs(pctl - 50.0), 2)


def max_realized_deviation(pctls: Sequence[float]) -> Optional[float]:
    """One game's Live re-rank score: the largest realized deviation among its lines.

    The single most notable tonight's-game-grain performance among a game's
    tracked players drives that game's Live-mode ordering (#541) -- not a
    sum across every tracked player, which would let a game with many
    merely-average lines outrank a game with one truly extreme one.

    Args:
        pctls: Every tracked player's tonight game-grain percentile in one
            game (any order -- the max is order-independent, so ties between
            e.g. a 90th- and a 10th-percentile line resolve identically
            regardless of input order).

    Returns:
        The largest :func:`realized_deviation_from_pctl` score, or ``None``
        when ``pctls`` is empty -- the caller's deterministic missing-line
        fallback (no tracked player has a resolved tonight's line yet, e.g.
        pretip) is to fall back to the game's entering weight, not to treat
        an empty game as a zero-deviation one.
    """
    if not pctls:
        return None
    return max(realized_deviation_from_pctl(p) for p in pctls)


# --------------------------------------------------------------------------- #
# Slate ranking (behavior spec §3 "Deviation-first per state", §5)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GameSlateInput:
    """One game's fired instances + context -- the input to :func:`rank_slate`."""

    game_id: int
    competition_id: int
    game_date: date
    status: str  # "scheduled" | "in_progress" | "final" | "unknown"
    tip_datetime: Optional[datetime]
    instances: Sequence[TriggerInstance] = field(default_factory=tuple)
    # Best (lowest-number) consensus/prominence rank among tonight's tracked
    # players in this game -- the Morning tiebreak (behavior spec §3).
    best_consensus_rank: Optional[int] = None
    # #541 -- this game's realized (tonight's, game-grain-ranked) Live
    # re-rank score (:func:`max_realized_deviation` over the game's tracked
    # players' tonight lines), or ``None`` when no tracked player has a
    # resolved tonight's line yet (pretip / no active game-grain baseline).
    # Takes priority over the pre-#541 trigger-instance-summed fallback in
    # :func:`rank_slate`'s live branch -- see that function's docstring.
    live_deviation: Optional[float] = None


@dataclass(frozen=True)
class SlateRow:
    """One ranked slate row -- maps cleanly onto ``DeskSlateRow`` (#506).

    ``total_weight`` is the mode-appropriate score: Morning's entering
    (additive) weight, or Live's realized-deviation-based re-rank score.
    """

    game_id: int
    competition_id: int
    game_date: date
    status: str
    tip_datetime: Optional[datetime]
    total_weight: float
    rank: int
    is_hero: bool
    instances: tuple[TriggerInstance, ...] = field(default_factory=tuple)


# Live-mode sort priority: in-progress games always outrank finished ones,
# regardless of weight (behavior spec §3 "finished games sink below
# in-progress ones"); not-yet-tipped games sit in between.
_LIVE_STATUS_PRIORITY: dict[str, int] = {
    "in_progress": 0,
    "scheduled": 1,
    "unknown": 1,
    "final": 2,
}


def rank_slate(
    games: Sequence[GameSlateInput], *, mode: Literal["morning", "live"]
) -> list[SlateRow]:
    """Rank a day's games by storyline weight, descending -- hero first.

    Morning: weight = additive sum of entering trigger weights; ties broken
    by best consensus rank among tracked players (behavior spec §3). Live:
    games re-rank by :attr:`GameSlateInput.live_deviation` -- tonight's
    realized game-grain deviation (#541, :func:`max_realized_deviation`) --
    with in-progress games always outranking finals (behavior spec §3
    "finished games sink below in-progress ones"). A game with
    ``live_deviation is None`` (no tracked player has a resolved tonight's
    line yet -- pretip, or no active game-grain baseline) falls back to the
    sum of its trigger instances' ``realized_deviation`` (the pre-#541
    status-heat-only signal), and if even that's empty, to the game's
    entering weight -- so a not-yet-live game never sorts as if it scored a
    fabricated zero. Sort is fully deterministic (``game_id`` as the final
    tiebreak) so re-running on identical input -- or on two games whose
    ``live_deviation`` ties exactly (e.g. a 90th- and a 10th-percentile line
    both scoring 40) -- always produces the identical ordering.

    Args:
        games: This day's games with their fired trigger instances.
        mode: ``"morning"`` (pre-tip, entering weight) or ``"live"``
            (realized-deviation re-rank).

    Returns:
        ``games`` mapped 1:1 to :class:`SlateRow`, sorted by rank ascending
        (rank 1 == the hero, ``is_hero=True``). Empty when ``games`` is
        empty.
    """
    entries: list[tuple[tuple[float, ...], GameSlateInput, float]] = []
    for g in games:
        entering_weight = round(sum(i.weight for i in g.instances), 2)
        rank_tiebreak = (
            float(g.best_consensus_rank)
            if g.best_consensus_rank is not None
            else math.inf
        )
        if mode == "morning":
            weight = entering_weight
            sort_key = (0.0, -weight, rank_tiebreak, float(g.game_id))
        else:
            if g.live_deviation is not None:
                weight = g.live_deviation
            else:
                realized = [
                    i.realized_deviation
                    for i in g.instances
                    if i.realized_deviation is not None
                ]
                weight = round(sum(realized), 2) if realized else entering_weight
            status_priority = float(_LIVE_STATUS_PRIORITY.get(g.status, 1))
            sort_key = (status_priority, -weight, rank_tiebreak, float(g.game_id))
        entries.append((sort_key, g, weight))

    entries.sort(key=lambda t: t[0])

    rows: list[SlateRow] = []
    for idx, (_key, g, weight) in enumerate(entries):
        rows.append(
            SlateRow(
                game_id=g.game_id,
                competition_id=g.competition_id,
                game_date=g.game_date,
                status=g.status,
                tip_datetime=g.tip_datetime,
                total_weight=weight,
                rank=idx + 1,
                is_hero=(idx == 0),
                instances=tuple(g.instances),
            )
        )
    return rows


def slate_needs_quiet_fallback(rows: Sequence[SlateRow]) -> bool:
    """Whether the slate is empty or nothing on it cleared a storyline threshold.

    Behavior spec §4: "no games today, or nothing clears the storyline
    threshold" both route to the quiet-slate hero fallback.
    """
    return not rows or all(r.total_weight <= 0 for r in rows)


# --------------------------------------------------------------------------- #
# Quiet-slate fallback hero (behavior spec §4 -- this ticket owns it)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClassLeaderCandidate:
    """One graded player, as input to the quiet-slate class-leader fallback."""

    player_id: int
    player_label: str
    pctl: float
    gmsc: float
    gated: bool = False


@dataclass(frozen=True)
class QuietSlateHero:
    """The always-force-a-headline fallback hero -- the class leader to date."""

    kind: Literal["class_leader"]
    player_id: int
    player_label: str
    pctl: float
    gmsc: float


def select_quiet_slate_hero(
    candidates: Sequence[ClassLeaderCandidate],
) -> Optional[QuietSlateHero]:
    """Pick the class leader / biggest mover-to-date when the slate is quiet.

    The front page must never have a dead hero (behavior spec §4). Ranks by
    cohort percentile first, raw GmSc as the tiebreak -- the same rule the
    Ledger's "Performance of the Night" hero uses (§4), so a quiet-slate
    fallback and a real Ledger night read consistently. Prefers ungraded-
    confident (non-``gated``) candidates; only falls back to a gated one when
    every candidate is gated (still better than no hero at all).

    Args:
        candidates: Every graded, tracked player to date this event.

    Returns:
        ``None`` only when ``candidates`` is empty (nothing to promote at
        all -- the caller's own "off-window"/no-data case, not this
        function's problem to solve).
    """
    if not candidates:
        return None
    ungated = [c for c in candidates if not c.gated]
    pool = ungated or list(candidates)
    winner = max(pool, key=lambda c: (c.pctl, c.gmsc))
    return QuietSlateHero(
        kind="class_leader",
        player_id=winner.player_id,
        player_label=winner.player_label,
        pctl=winner.pctl,
        gmsc=winner.gmsc,
    )


# --------------------------------------------------------------------------- #
# Async orchestration -- Job B step 3 (writes T3 + T4)
# --------------------------------------------------------------------------- #
_ROSTER_ACTIVE_STATUSES = {
    AffiliationStatus.ANNOUNCED,
    AffiliationStatus.CONFIRMED,
    AffiliationStatus.ACTIVE,
}


@dataclass(frozen=True)
class StorylineTickResult:
    """The result of one :func:`compute_desk_storylines` call."""

    slate: list[SlateRow]
    # Populated only when :func:`slate_needs_quiet_fallback` is true for
    # ``slate`` -- a quiet slate has no game to attach a hero to.
    quiet_slate_hero: Optional[QuietSlateHero] = None


async def _consensus_rank_map(
    session: AsyncSession, player_ids: Sequence[int]
) -> dict[int, int]:
    """Each player's most recent DraftGuru consensus rank, across any snapshot.

    "Most recent snapshot the player appears in" is this module's working
    definition of "our canonical board" (behavior spec §3) -- simpler and
    more robust than trying to infer which ``draft_year`` board a given SL
    roster player belongs to (rookies, sophomores, and undrafted invitees all
    mix on one night's slate).
    """
    if not player_ids:
        return {}
    # Local import: avoids a hard dependency edge from this module onto the
    # consensus schema for callers that never touch prominence.
    from app.schemas.consensus import BigBoardConsensus, ConsensusSnapshot

    stmt = (
        select(  # type: ignore[call-overload]
            BigBoardConsensus.player_id,
            BigBoardConsensus.consensus_rank,
            ConsensusSnapshot.computed_at,
        )
        .join(ConsensusSnapshot, ConsensusSnapshot.id == BigBoardConsensus.snapshot_id)
        .where(BigBoardConsensus.player_id.in_(player_ids))  # type: ignore[attr-defined]
        .order_by(
            BigBoardConsensus.player_id,
            ConsensusSnapshot.computed_at.desc(),  # type: ignore[attr-defined]
        )
    )
    rows = (await session.execute(stmt)).all()
    out: dict[int, int] = {}
    for pid, rank, _computed_at in rows:
        if pid not in out:
            out[pid] = rank
    return out


async def compute_desk_storylines(
    session: AsyncSession,
    *,
    game_date: date,
    competition_id: int,
    baseline_version: str,
    mode: Literal["morning", "live"] = "morning",
) -> StorylineTickResult:
    """Evaluate all five triggers for one day's games and upsert T3 + T4.

    Job B step 3 (behavior spec §10): reads tonight's schedule + rosters,
    consensus rank, T2 grades, and each tracked player's prior-competition
    game log, fires the five detectors per game, ranks the resulting slate
    (:func:`rank_slate`), and writes the outcome to
    ``summer_league_desk_storylines`` (T3, full replace for the touched
    games) and ``summer_league_desk_slate`` (T4, upsert by ``game_id``).
    When the resulting slate needs the quiet-slate fallback
    (:func:`slate_needs_quiet_fallback`), also resolves and returns the
    class-leader hero -- callers attach it wherever the hero belongs (there's
    no game row to hang it on).

    Does not commit; the caller controls the transaction (matches
    ``grade_player_event`` / ``build_baselines``).

    Args:
        session: Active database session.
        game_date: The day's slate to evaluate.
        competition_id: Scopes the games, rosters, and T2/T4 rows.
        baseline_version: Which T1 ``baseline_version`` T2 grades were
            written against (also used to look up event-grain baselines for
            the streak/2nd-look approximations).
        mode: ``"morning"`` (entering weight) or ``"live"`` (realized
            deviation re-rank) -- forwarded to :func:`rank_slate`.

    Returns:
        The ranked slate plus an optional quiet-slate fallback hero.
    """
    competition = await session.get(SummerLeagueCompetition, competition_id)
    if competition is None:
        raise ValueError(f"No summer_league_competitions row for id={competition_id}.")

    games = (
        (
            await session.execute(
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
        no_games_quiet_hero = await select_quiet_slate_hero_from_grades(
            session, competition_id=competition_id, baseline_version=baseline_version
        )
        return StorylineTickResult(slate=[], quiet_slate_hero=no_games_quiet_hero)

    team_entry_ids = {
        tid
        for g in games
        for tid in (g.home_team_entry_id, g.away_team_entry_id)
        if tid is not None
    }
    roster_rows = (
        (
            await session.execute(
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
    all_player_ids: set[int] = set()
    for team_entry_id, player_id, roster_status in roster_rows:
        if roster_status not in _ROSTER_ACTIVE_STATUSES:
            continue
        roster_by_team.setdefault(team_entry_id, []).append(player_id)
        all_player_ids.add(player_id)

    players = (
        (
            await session.execute(
                select(PlayerMaster).where(  # type: ignore[call-overload]
                    PlayerMaster.id.in_(all_player_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
        if all_player_ids
        else []
    )
    player_by_id = {p.id: p for p in players if p.id is not None}

    consensus_rank_map = await _consensus_rank_map(session, list(all_player_ids))

    # The ONE batched player_id -> first-qualifying-game lookup (#539), shared
    # with the fact path (`desk_fact_queries.fetch_debut_status`) and Job A's
    # debut-grain baseline (`cohort_baselines.first_qualifying_games`) --
    # never "no prior-year log" (the pre-#539 approximation that fired the
    # Debut trigger on every game of a debut season instead of just the
    # first one).
    first_qualifying_by_player: dict[
        int, FirstQualifyingGame
    ] = await fetch_first_qualifying_games(session, player_ids=list(all_player_ids))

    grades = (
        (
            await session.execute(
                select(SummerLeagueDeskPlayerGrade).where(
                    SummerLeagueDeskPlayerGrade.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.player_id.in_(all_player_ids),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
        if all_player_ids
        else []
    )
    grade_by_player: dict[int, GradeRow] = {
        g.player_id: GradeRow(
            player_id=g.player_id,
            competition_id=g.competition_id,
            baseline_version=g.baseline_version,
            cohort_key=g.cohort_key,
            subject_value=g.subject_value,
            pctl=g.pctl,
            grade=g.grade,
            n_cohort=g.n_cohort,
            gated=g.gated,
        )
        for g in grades
    }

    cohort_keys = {g.cohort_key for g in grade_by_player.values()}
    baselines: dict[str, SummerLeagueCohortBaseline] = {}
    if cohort_keys:
        baseline_rows = (
            (
                await session.execute(
                    select(SummerLeagueCohortBaseline).where(
                        SummerLeagueCohortBaseline.baseline_version == baseline_version,  # type: ignore[arg-type]
                        SummerLeagueCohortBaseline.cohort_key.in_(cohort_keys),  # type: ignore[attr-defined]
                        SummerLeagueCohortBaseline.grain == SummerLeagueDeskGrain.EVENT,  # type: ignore[arg-type]
                        SummerLeagueCohortBaseline.is_active.is_(True),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        baselines = {row.cohort_key: row for row in baseline_rows}

    # Game-grain baselines back the streak trigger's per-game bar/percentile
    # (#525) -- a separate fetch since the streak trigger measures individual
    # games against a game-grain distribution while `baselines` above (event
    # grain) still backs the 2nd-look trigger's event-aggregate comparison.
    game_cohort_key_by_player: dict[int, str] = {
        pid: cohort_key_for(
            p.draft_round, p.draft_pick, grain=SummerLeagueDeskGrain.GAME
        )
        for pid, p in player_by_id.items()
    }
    game_cohort_keys = set(game_cohort_key_by_player.values())
    game_baselines: dict[str, SummerLeagueCohortBaseline] = {}
    if game_cohort_keys:
        game_baseline_rows = (
            (
                await session.execute(
                    select(SummerLeagueCohortBaseline).where(
                        SummerLeagueCohortBaseline.baseline_version == baseline_version,  # type: ignore[arg-type]
                        SummerLeagueCohortBaseline.cohort_key.in_(game_cohort_keys),  # type: ignore[attr-defined]
                        SummerLeagueCohortBaseline.grain == SummerLeagueDeskGrain.GAME,  # type: ignore[arg-type]
                        SummerLeagueCohortBaseline.is_active.is_(True),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        game_baselines = {row.cohort_key: row for row in game_baseline_rows}

    # #548 -- batch every remaining per-player storyline context UP FRONT (one
    # query each, never once per slot) so the per-game/per-slot loop below is
    # entirely DB-free trigger evaluation. Formerly per-player awaits
    # (``_game_lines_before`` / ``_prior_event`` / ``_current_event_gp``,
    # removed) that grew this call's query count linearly with roster size.
    baseline_by_player: dict[int, Optional[SummerLeagueCohortBaseline]] = {
        pid: game_baselines.get(game_cohort_key_by_player[pid]) for pid in player_by_id
    }
    game_lines_by_player = await fetch_game_lines(
        session,
        player_ids=list(all_player_ids),
        competition_id=competition_id,
        game_date=game_date,
        baseline_by_player=baseline_by_player,
        inclusive=False,
    )
    prior_events_by_player = await fetch_prior_events(
        session, player_ids=list(all_player_ids), before_year=competition.year
    )
    current_gp_by_player = await fetch_current_event_gp(
        session, player_ids=list(all_player_ids), year=competition.year
    )

    # #541 -- tonight's realized (game-grain) lines back Live-mode ordering.
    # ONE batched query for the whole night's slate (never per-game/per-player):
    # every resolved log line any tracked player has logged in TODAY's games,
    # grouped by game_id below. Only fetched in "live" mode -- Morning has
    # nothing to rank a realized deviation against yet.
    tonight_logs_by_game: dict[int, list[SummerLeaguePlayerGameLog]] = defaultdict(list)
    if mode == "live" and all_player_ids:
        touched_game_ids = [g.id for g in games if g.id is not None]
        tonight_log_rows = (
            (
                await session.execute(
                    select(SummerLeaguePlayerGameLog).where(
                        SummerLeaguePlayerGameLog.competition_id == competition_id,  # type: ignore[arg-type]
                        SummerLeaguePlayerGameLog.game_id.in_(touched_game_ids),  # type: ignore[attr-defined]
                        SummerLeaguePlayerGameLog.player_id.in_(all_player_ids),  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
        )
        for log_row in tonight_log_rows:
            if log_row.player_id is None:
                continue
            tonight_logs_by_game[log_row.game_id].append(log_row)

    game_inputs: list[GameSlateInput] = []
    new_storyline_rows: list[SummerLeagueDeskStoryline] = []

    for g in games:
        assert g.id is not None
        roster_ids = list(
            dict.fromkeys(
                roster_by_team.get(g.home_team_entry_id or -1, [])
                + roster_by_team.get(g.away_team_entry_id or -1, [])
            )
        )
        roster_id_set = set(roster_ids)

        slots: list[ProspectSlot] = []
        for pid in roster_ids:
            player = player_by_id.get(pid)
            if player is None:
                continue
            slots.append(
                ProspectSlot(
                    player_id=pid,
                    player_label=player.display_name or f"Player {pid}",
                    draft_round=player.draft_round,
                    draft_pick=player.draft_pick,
                    consensus_rank=consensus_rank_map.get(pid),
                )
            )

        # Live mode drops confirmed non-participants: a player with a box row
        # for THIS game that shows no minutes (a DNP roster shell) did not play,
        # so he is not an eligible storyline subject. Without this a rostered
        # veteran who dressed but sat (e.g. Cam Reddish) still fires a Debut/
        # Second-look trigger off his prior-year history and can outweigh
        # everyone who played, hijacking the Live hero. A player with NO row yet
        # is merely pre-tip -- kept, so an in-progress Duel between a logged
        # prospect and a not-yet-checked-in one still fires (#541). Morning mode
        # keeps the full active roster (its framing is a pre-tip prediction).
        if mode == "live":
            dnp_shell_ids = {
                log.player_id
                for log in tonight_logs_by_game.get(g.id, [])
                if log.player_id is not None
                and not (log.minutes_seconds is not None and log.minutes_seconds > 0)
            }
            slots = [slot for slot in slots if slot.player_id not in dnp_shell_ids]

        instances: list[TriggerInstance] = []

        duel = detect_duel(candidates=slots)
        if duel is not None:
            instances.append(duel)

        for slot in slots:
            grade = grade_by_player.get(slot.player_id)

            # Debut fires when the subject has no EARLIER qualifying game on
            # record (#539): either they've never once cleared the per-game
            # floor yet (``first_qualifying is None`` -- covers the Morning/
            # Preview prediction case, before tonight's game has even been
            # played) or the one qualifying game they DO have on record IS
            # this one (``g``). A player whose earliest qualifying game
            # points at some OTHER game -- an earlier game this same event,
            # or any prior year -- has already debuted, so `g` (whatever it
            # is) can never re-trigger Debut. This is what stops a debut
            # season's 2nd/3rd/... game (and every sophomore's game) from
            # re-firing the trigger the old "no prior *year* of history"
            # check used to.
            first_qualifying = first_qualifying_by_player.get(slot.player_id)
            is_debut = first_qualifying is None or first_qualifying.game_id == g.id
            debut = detect_debut(subject=slot, is_debut=is_debut)
            if debut is not None:
                instances.append(debut)

            status_heat = detect_status_heat(subject=slot, grade=grade)
            if status_heat is not None:
                instances.append(status_heat)

            if grade is not None:
                baseline = baselines.get(grade.cohort_key)
                game_cohort_key = game_cohort_key_by_player.get(
                    slot.player_id
                ) or cohort_key_for(
                    slot.draft_round, slot.draft_pick, grain=SummerLeagueDeskGrain.GAME
                )
                # DB-free (#548): both context reads below are pure dict
                # lookups off the batched fetches above, not per-slot awaits.
                lines = game_lines_by_player.get(slot.player_id, [])
                streak = detect_streak(
                    subject=slot, cohort_key=game_cohort_key, games=lines
                )
                if streak is not None:
                    instances.append(streak)

                prior = prior_events_by_player.get(slot.player_id)
                if prior is not None:
                    current_gp = current_gp_by_player.get(slot.player_id, 0)
                    current_pctl = grade.pctl
                    prior_pctl: Optional[float] = None
                    if baseline is not None:
                        try:
                            prior_pctl = percentile_of_value(
                                baseline.breakpoints, prior.value
                            )
                        except ValueError:
                            prior_pctl = None
                    second_look = detect_second_look(
                        subject=slot,
                        current_value=grade.subject_value,
                        current_gp=current_gp,
                        prior=prior,
                        current_pctl=current_pctl,
                        prior_pctl=prior_pctl,
                        gated=grade.gated,
                    )
                    if second_look is not None:
                        instances.append(second_look)

        best_rank: Optional[int] = None
        for slot in slots:
            rank = effective_prominence_rank(
                slot.consensus_rank, slot.draft_round, slot.draft_pick
            )
            if rank is not None and (best_rank is None or rank < best_rank):
                best_rank = rank

        # #541 -- this game's realized Live re-rank score: every tracked
        # (active-roster) player's TONIGHT line, ranked through the active
        # game-grain cohort baseline. A player without an active game-grain
        # baseline for their cohort, or with no resolved line yet, simply
        # doesn't contribute a percentile -- `max_realized_deviation` returns
        # `None` (not a fabricated 0) when nobody in the game has one yet,
        # which `rank_slate` treats as "fall back to entering weight."
        live_deviation: Optional[float] = None
        if mode == "live":
            game_pctls: list[float] = []
            for log_row in tonight_logs_by_game.get(g.id, []):
                if log_row.player_id is None or log_row.player_id not in roster_id_set:
                    continue
                player = player_by_id.get(log_row.player_id)
                if player is None:
                    continue
                game_baseline = game_baselines.get(
                    cohort_key_for(
                        player.draft_round,
                        player.draft_pick,
                        grain=SummerLeagueDeskGrain.GAME,
                    )
                )
                if game_baseline is None:
                    continue
                gmsc = round(game_score_from_row(log_row), 2)
                try:
                    game_pctls.append(
                        percentile_of_value(game_baseline.breakpoints, gmsc)
                    )
                except ValueError:
                    continue
            live_deviation = max_realized_deviation(game_pctls)

        game_inputs.append(
            GameSlateInput(
                game_id=g.id,
                competition_id=competition_id,
                game_date=g.game_date or game_date,
                status=g.status.value,
                tip_datetime=g.tip_datetime,
                instances=tuple(instances),
                best_consensus_rank=best_rank,
                live_deviation=live_deviation,
            )
        )

        for inst in instances:
            new_storyline_rows.append(
                SummerLeagueDeskStoryline(
                    game_date=g.game_date or game_date,
                    competition_id=competition_id,
                    game_id=g.id,
                    trigger_type=inst.trigger_type,
                    subject_player_id=inst.subject_player_id,
                    subject_player_id_2=inst.subject_player_id_2,
                    base_weight=inst.base_weight,
                    magnitude=inst.magnitude,
                    weight=inst.weight,
                    realized_deviation=inst.realized_deviation,
                )
            )

    slate = rank_slate(game_inputs, mode=mode)

    # Full replace of T3 for the games touched this tick -- every row here is
    # a rebuildable projection (module docstring); simplest correct upsert.
    touched_game_ids = [g.id for g in games if g.id is not None]
    if touched_game_ids:
        await session.execute(
            delete(SummerLeagueDeskStoryline).where(
                SummerLeagueDeskStoryline.game_id.in_(touched_game_ids)  # type: ignore[attr-defined]
            )
        )
    for row in new_storyline_rows:
        session.add(row)

    for slate_row in slate:
        values = {
            "game_date": slate_row.game_date,
            "competition_id": slate_row.competition_id,
            "game_id": slate_row.game_id,
            "total_weight": slate_row.total_weight,
            "rank": slate_row.rank,
            "is_hero": slate_row.is_hero,
            "computed_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        stmt = insert(SummerLeagueDeskSlate).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_summer_league_desk_slate_game",
            set_={
                "total_weight": values["total_weight"],
                "rank": values["rank"],
                "is_hero": values["is_hero"],
                "computed_at": values["computed_at"],
            },
        )
        await session.execute(stmt)

    await session.flush()

    quiet_hero: Optional[QuietSlateHero] = None
    if slate_needs_quiet_fallback(slate):
        quiet_hero = await select_quiet_slate_hero_from_grades(
            session, competition_id=competition_id, baseline_version=baseline_version
        )

    return StorylineTickResult(slate=slate, quiet_slate_hero=quiet_hero)


async def select_quiet_slate_hero_from_grades(
    session: AsyncSession, *, competition_id: int, baseline_version: str
) -> Optional[QuietSlateHero]:
    """Fetch every T2 grade for the event and pick the quiet-slate fallback hero.

    Args:
        session: Active database session.
        competition_id: Scopes the T2 grades read.
        baseline_version: Which T1 ``baseline_version`` the grades were
            written against.

    Returns:
        ``None`` when nobody has been graded yet this event (nothing to
        promote -- this is expected on the very first tick before any T2
        rows exist, e.g. off-window).
    """
    rows = (
        (
            await session.execute(
                select(SummerLeagueDeskPlayerGrade).where(
                    SummerLeagueDeskPlayerGrade.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    player_ids = [r.player_id for r in rows]
    players = (
        (
            await session.execute(
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
            player_id=r.player_id,
            player_label=label_by_id.get(r.player_id, f"Player {r.player_id}"),
            pctl=r.pctl,
            gmsc=r.subject_value,
            gated=r.gated,
        )
        for r in rows
    ]
    return select_quiet_slate_hero(candidates)


__all__ = [
    "BASE_WEIGHTS",
    "DUEL_PROMINENCE_CUTOFF",
    "STATUS_HEAT_PCTL_FLOOR",
    "ClassLeaderCandidate",
    "GameSlateInput",
    "ProspectSlot",
    "QuietSlateHero",
    "SlateRow",
    "StorylineTickResult",
    "TriggerInstance",
    "base_weight",
    "compute_desk_storylines",
    "detect_debut",
    "detect_duel",
    "detect_second_look",
    "detect_status_heat",
    "detect_streak",
    "draft_slot_fallback",
    "effective_prominence_rank",
    "max_realized_deviation",
    "prominence_score",
    "rank_slate",
    "realized_deviation_from_pctl",
    "select_quiet_slate_hero",
    "select_quiet_slate_hero_from_grades",
    "slate_needs_quiet_fallback",
]

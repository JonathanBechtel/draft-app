"""Stage 1 of the Summer League Desk commentary pipeline: the fact library.

The commentary engine is a deterministic **fact -> angle -> phrase** pipeline
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §11): typed Fact
detectors (this module, Stage 1) feed a notability/selection pass (Stage 2,
#518) that feeds a template realizer (Stage 3, #519). **This module renders
no prose** -- it only detects facts and scores them for later selection.

Every detector is one reproducible query/audit unit: `(subject, context) ->
Fact | None`, per spec. To keep detectors pure and unit-testable without a
database, each one takes **already-fetched, plain-data inputs** (the
dataclasses below) rather than an ``AsyncSession`` -- #518/#519 are
responsible for fetching those inputs from T1/T2 (`cohort_baselines.py`,
`desk_grades.py`) and raw game logs, then calling these functions.

**Streak's per-game percentile source:** ``detect_streak`` is the one
detector that conceptually needs a per-game cohort percentile (spec: "the
run's average percentile >= 65"). Its input shape (:class:`GameLine`) carries
an *optional* ``pctl`` field so the detector stays pure/DB-free -- callers
(`desk_storylines.py`, `desk_fact_queries.fetch_game_lines`) supply it from
#525's ``game``-grain T1 baseline (`cohort_baselines.build_baselines`,
pooling every qualifying individual-game GmSc per cohort). A caller without
an active game-grain baseline row for the subject's cohort passes
``pctl=None``; the detector honestly declines to fire a streak through any
game lacking one rather than guessing.

Provenance: every emitted :class:`Fact` carries a
:class:`FactProvenance` (``detector_id``, ``baseline_version``,
``cohort_key``) so a rendered sentence can always be traced back to the
query that produced it (spec §8's "every displayed sentence must trace to
a query").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence

from app.services.sources.summer_league.desk_grades import GradeRow

# A gated GradeRow (adaptive gate-ladder-suppressed percentile, #503) should
# generally suppress confident claims -- `detect_percentile` dampens
# notability by this factor rather than refusing to emit a Fact outright,
# since a grade chip renders regardless of gating (spec §11 Stage 2: "grade
# chips render regardless (a chip is not prose)"); only *prose* selection
# (out of scope here, #518) needs to actually respect the notability floor.
GATED_NOTABILITY_DAMPING = 0.3


def _clamp01(x: float) -> float:
    """Clamp ``x`` into ``[0.0, 1.0]`` -- the ``notability`` score's range."""
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------- #
# The Fact value object (spec §11 Stage 1)
# --------------------------------------------------------------------------- #
class FactKind(str, Enum):
    """The eight SL fact kinds V1 needs (behavior spec §11 table)."""

    COHORT_RANK = "cohort_rank"
    PERCENTILE = "percentile"
    STREAK = "streak"
    SELF_DELTA = "self_delta"
    LEADS_FIELD = "leads_field"
    DEBUT_VS_BAR = "debut_vs_bar"
    COUNT_CLUB = "count_club"
    FIRST_SINCE = "first_since"


@dataclass(frozen=True)
class FactSubject:
    """Who a Fact is about: a player, optionally pinned to one event.

    Callers build this from whatever they already fetched (e.g. a
    :class:`~app.services.sources.summer_league.desk_grades.GradeRow`'s
    ``player_id``/``competition_id`` plus a display name from
    ``players_master``) -- detectors never look players up themselves.
    """

    player_id: int
    player_label: str
    competition_id: Optional[int] = None


@dataclass(frozen=True)
class RunnerUp:
    """The comparison point a rank/lead Fact is measured against."""

    who: str
    value: float


@dataclass(frozen=True)
class FactProvenance:
    """Reproducibility/audit trail for one emitted Fact (spec §11, §8)."""

    detector_id: str
    baseline_version: Optional[str] = None
    cohort_key: Optional[str] = None


@dataclass(frozen=True)
class Fact:
    """A typed, provenance-carrying record of one detected commentary fact.

    Mirrors the behavior spec §11 Stage 1 shape exactly:
    ``kind / subject / metric / cohort / values / notability / provenance``.
    Stage 2 (#518) selects among fired Facts by ``notability``; Stage 3
    (#519) renders selected Facts to prose via a template registry keyed on
    ``kind``; T2/T4's ``facts`` JSONB columns persist the selection via
    :meth:`to_dict`.
    """

    kind: FactKind
    subject: FactSubject
    metric: str
    cohort: Optional[str]
    values: dict[str, Any]
    notability: float
    provenance: FactProvenance

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-clean dict for the T2/T4 ``facts`` JSONB columns."""
        return {
            "kind": self.kind.value,
            "subject": {
                "player_id": self.subject.player_id,
                "player_label": self.subject.player_label,
                "competition_id": self.subject.competition_id,
            },
            "metric": self.metric,
            "cohort": self.cohort,
            "values": self.values,
            "notability": self.notability,
            "provenance": {
                "detector_id": self.provenance.detector_id,
                "baseline_version": self.provenance.baseline_version,
                "cohort_key": self.provenance.cohort_key,
            },
        }


# --------------------------------------------------------------------------- #
# cohort_rank
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CohortPeer:
    """One other member of a comparison population (not the subject)."""

    label: str
    value: float


def detect_cohort_rank(
    *,
    subject: FactSubject,
    subject_value: float,
    metric: str,
    cohort_key: str,
    peers: Sequence[CohortPeer],
    baseline_version: Optional[str] = None,
    higher_is_better: bool = True,
) -> Optional[Fact]:
    """Rank ``subject_value`` within a historical cohort distribution.

    Mockup example: "best start by a #1 pick ... ahead of 2025 Flagg
    (18.9)" -- ``peers`` is the caller-fetched historical population for
    the subject's cohort (e.g. every other #1 pick's debut GmSc), NOT
    including the subject. ``runner_up`` is always the best-of-the-rest
    peer, independent of the subject's own rank, so the Fact can phrase
    either "ahead of X" (subject is #1) or "trails X" (subject isn't).

    Args:
        subject: Who the fact is about.
        subject_value: The subject's value on ``metric``.
        metric: e.g. ``"gmsc"``.
        cohort_key: The T1-style cohort key this ranking is scoped to.
        peers: The rest of the comparison population (subject excluded).
        baseline_version: The T1 ``baseline_version`` ``peers`` was read
            from, if any (informational; this detector doesn't touch T1
            directly -- it just ranks the values it's given).
        higher_is_better: ``False`` for metrics where a lower value ranks
            better (e.g. turnovers).

    Returns:
        ``None`` when ``peers`` is empty (nothing to rank against).
    """
    if not peers:
        return None

    if higher_is_better:
        better = sum(1 for p in peers if p.value > subject_value)
        runner_up = max(peers, key=lambda p: p.value)
    else:
        better = sum(1 for p in peers if p.value < subject_value)
        runner_up = min(peers, key=lambda p: p.value)

    rank = better + 1
    of = len(peers) + 1
    notability = _clamp01(1.0 - (rank - 1) / of)

    return Fact(
        kind=FactKind.COHORT_RANK,
        subject=subject,
        metric=metric,
        cohort=cohort_key,
        values={
            "value": subject_value,
            "rank": rank,
            "of": of,
            "runner_up": {"who": runner_up.label, "value": runner_up.value},
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.COHORT_RANK.value,
            baseline_version=baseline_version,
            cohort_key=cohort_key,
        ),
    )


# --------------------------------------------------------------------------- #
# percentile
# --------------------------------------------------------------------------- #
def detect_percentile(
    *,
    subject: FactSubject,
    grade: GradeRow,
    metric: str = "gmsc",
) -> Fact:
    """Wrap a graded outcome (T2 :class:`GradeRow`, #503) as a Fact.

    Reuses #503's percentile/grade rather than recomputing anything --
    this detector is a thin, always-firing adapter (a grade chip renders
    regardless of extremity or gating per spec §11 Stage 2; only prose
    *selection*, out of scope here, filters on the notability floor).
    Extremity scores highest (near 0th/100th percentile); mid-pack
    (~50th) scores lowest. A :attr:`GradeRow.gated` outcome has its
    notability dampened by :data:`GATED_NOTABILITY_DAMPING` -- a gated
    percentile shouldn't read as a confident superlative.

    Args:
        subject: Who the fact is about.
        grade: The T2 outcome to wrap.
        metric: The metric the percentile was computed on (T2 grades are
            always GmSc today; kept as a param for forward compatibility).

    Returns:
        Always a :class:`Fact` (never ``None``).
    """
    extremity = abs(grade.pctl - 50.0) / 50.0
    notability = _clamp01(extremity)
    if grade.gated:
        notability = _clamp01(notability * GATED_NOTABILITY_DAMPING)

    return Fact(
        kind=FactKind.PERCENTILE,
        subject=subject,
        metric=metric,
        cohort=grade.cohort_key,
        values={
            "value": grade.subject_value,
            "pctl": grade.pctl,
            "n_cohort": grade.n_cohort,
            "gated": grade.gated,
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.PERCENTILE.value,
            baseline_version=grade.baseline_version,
            cohort_key=grade.cohort_key,
        ),
    )


# --------------------------------------------------------------------------- #
# streak
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GameLine:
    """One game in a player's chronological SL log, pre-scored vs a cohort.

    ``pctl`` is populated from a **game-grain** T1 cohort baseline (#525,
    `cohort_baselines.py` grain=``game`` -- see module docstring). Callers
    without an active baseline row for the subject's cohort should pass
    ``pctl=None``; :func:`detect_streak` treats that as "can't extend the run
    through this game" rather than guessing.
    """

    value: float
    cohort_median: float
    pctl: Optional[float] = None
    game_id: Optional[int] = None
    label: Optional[str] = None


MIN_STREAK_LENGTH = 3
STREAK_AVG_PCTL_FLOOR = 65.0


def detect_streak(
    *,
    subject: FactSubject,
    metric: str,
    cohort_key: Optional[str],
    games: Sequence[GameLine],
    baseline_version: Optional[str] = None,
) -> Optional[Fact]:
    """An active run of >=3 straight games clearing the cohort median.

    Pinned decision (spec §3 / "Pinned decisions" #3): >=3 straight SL
    games each at/above the player's cohort-median GmSc, AND the run's
    average percentile >= 65. Walks ``games`` backwards from the most
    recent game (``games`` must be chronological, oldest first),
    extending the active run while each game clears its cohort median and
    carries a known percentile; a game below the median, or one with
    ``pctl is None`` (see :class:`GameLine`), stops the run.

    Args:
        subject: Who the fact is about.
        metric: e.g. ``"gmsc"``.
        cohort_key: The cohort the median/percentiles are scoped to.
        games: The player's SL game log, oldest first.
        baseline_version: Informational T1/game-baseline version tag.

    Returns:
        ``None`` when the trailing run is shorter than
        :data:`MIN_STREAK_LENGTH` games or its average percentile is
        below :data:`STREAK_AVG_PCTL_FLOOR`.
    """
    run: list[GameLine] = []
    for g in reversed(games):
        if g.pctl is None or g.value < g.cohort_median:
            break
        run.append(g)

    if len(run) < MIN_STREAK_LENGTH:
        return None

    pctls = [g.pctl for g in run if g.pctl is not None]
    avg_pctl = sum(pctls) / len(pctls)
    if avg_pctl < STREAK_AVG_PCTL_FLOOR:
        return None

    length = len(run)
    notability = _clamp01(
        (avg_pctl / 100.0) * (1 + 0.05 * (length - MIN_STREAK_LENGTH))
    )

    return Fact(
        kind=FactKind.STREAK,
        subject=subject,
        metric=metric,
        cohort=cohort_key,
        values={
            "length": length,
            "avg_pctl": round(avg_pctl, 2),
            "avg_value": round(sum(g.value for g in run) / length, 2),
            "min_value": round(min(g.value for g in run), 2),
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.STREAK.value,
            baseline_version=baseline_version,
            cohort_key=cohort_key,
        ),
    )


# --------------------------------------------------------------------------- #
# self_delta
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PriorEvent:
    """A subject's own prior-year SL event-aggregate."""

    year: int
    value: float
    gp: int


NOTABLE_SELF_DELTA_FLOOR = 3.0


def detect_self_delta(
    *,
    subject: FactSubject,
    metric: str,
    cohort_key: Optional[str],
    current_value: float,
    current_gp: int,
    prior: Optional[PriorEvent],
    baseline_version: Optional[str] = None,
    notable_delta_floor: float = NOTABLE_SELF_DELTA_FLOOR,
) -> Optional[Fact]:
    """A subject's current event vs their own prior-year SL.

    Mockup example: "+5.3 GmSc ahead of his first summer".

    Args:
        subject: Who the fact is about.
        metric: e.g. ``"gmsc"``.
        cohort_key: Informational cohort tag carried onto the Fact.
        current_value: This event's value.
        current_gp: This event's games played.
        prior: The subject's prior-year event-aggregate, or ``None`` for a
            debutant (use :func:`detect_debut_vs_bar` instead -- a
            debutant has no prior year to delta against).
        baseline_version: Informational version tag.
        notable_delta_floor: Minimum ``abs(delta)`` to be worth a Fact.

    Returns:
        ``None`` when there's no ``prior`` event, or the swing is smaller
        than ``notable_delta_floor``.
    """
    if prior is None:
        return None

    delta = round(current_value - prior.value, 2)
    if abs(delta) < notable_delta_floor:
        return None

    notability = _clamp01(abs(delta) / (notable_delta_floor * 4))

    return Fact(
        kind=FactKind.SELF_DELTA,
        subject=subject,
        metric=metric,
        cohort=cohort_key,
        values={
            "value": current_value,
            "gp": current_gp,
            "delta": delta,
            "since_year": prior.year,
            "prior_value": prior.value,
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.SELF_DELTA.value,
            baseline_version=baseline_version,
            cohort_key=cohort_key,
        ),
    )


# --------------------------------------------------------------------------- #
# leads_field
# --------------------------------------------------------------------------- #
def detect_leads_field(
    *,
    subject: FactSubject,
    subject_value: float,
    metric: str,
    field_label: str,
    field: Sequence[CohortPeer],
    higher_is_better: bool = True,
) -> Optional[Fact]:
    """Whether the subject leads a same-night/live field.

    Mockup example: "leads all rookies tonight"; "top undrafted performer".
    Unlike :func:`detect_cohort_rank` (a historical distribution),
    ``field`` is a live, same-night snapshot the caller assembles (e.g.
    every other rookie playing tonight) -- NOT including the subject.
    This detector only fires a Fact when the subject actually leads the
    field (rank == 1); for any other rank use :func:`detect_cohort_rank`.

    Args:
        subject: Who the fact is about.
        subject_value: The subject's value on ``metric``.
        metric: e.g. ``"gmsc"``.
        field_label: What the field represents (e.g. ``"rookies"``,
            ``"undrafted players"``) -- becomes the Fact's ``cohort`` as
            ``field:{field_label}``.
        field: The rest of the live field (subject excluded).
        higher_is_better: ``False`` for metrics where lower is better.

    Returns:
        ``None`` when ``field`` is empty, or the subject doesn't actually
        lead it.
    """
    if not field:
        return None

    if higher_is_better:
        leads = all(subject_value >= p.value for p in field)
        runner_up = max(field, key=lambda p: p.value)
    else:
        leads = all(subject_value <= p.value for p in field)
        runner_up = min(field, key=lambda p: p.value)

    if not leads:
        return None

    of = len(field) + 1
    notability = _clamp01(0.6 + 0.4 * _clamp01((of - 1) / 10.0))
    cohort = f"field:{field_label}"

    return Fact(
        kind=FactKind.LEADS_FIELD,
        subject=subject,
        metric=metric,
        cohort=cohort,
        values={
            "value": subject_value,
            "rank": 1,
            "of": of,
            "runner_up": {"who": runner_up.label, "value": runner_up.value},
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.LEADS_FIELD.value,
            baseline_version=None,
            cohort_key=cohort,
        ),
    )


# --------------------------------------------------------------------------- #
# debut_vs_bar
# --------------------------------------------------------------------------- #
def detect_debut_vs_bar(
    *,
    subject: FactSubject,
    metric: str,
    debut_cohort_key: str,
    subject_value: float,
    debut_bar: float,
    baseline_version: Optional[str] = None,
) -> Fact:
    """A debut event vs the cohort's historical debut bar (T1 ``debut`` grain).

    Spec §6: "Debut bar = cohort mean GmSc for that slot/status cohort's
    first-ever SL games" -- ``debut_bar`` is the caller-fetched T1 debut-
    grain baseline row's ``mean_value`` for the subject's cohort.

    Args:
        subject: Who the fact is about.
        metric: e.g. ``"gmsc"``.
        debut_cohort_key: The ``debut:...`` cohort key (#502 convention).
        subject_value: The subject's debut event value.
        debut_bar: The cohort's historical debut mean.
        baseline_version: The T1 ``baseline_version`` ``debut_bar`` came
            from.

    Returns:
        Always a :class:`Fact` -- a debut is inherently notable (the
        storyline engine's ``debut`` trigger has base weight 80
        regardless); the delta only tunes how strongly it reads. A debut
        exactly at the bar (``delta == 0``) is the boundary case, scoring
        the formula's 0.5 floor.
    """
    delta = round(subject_value - debut_bar, 2)
    span = abs(debut_bar) * 2 or 1.0
    notability = _clamp01(0.5 + delta / span)

    return Fact(
        kind=FactKind.DEBUT_VS_BAR,
        subject=subject,
        metric=metric,
        cohort=debut_cohort_key,
        values={
            "value": subject_value,
            "bar": debut_bar,
            "delta": delta,
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.DEBUT_VS_BAR.value,
            baseline_version=baseline_version,
            cohort_key=debut_cohort_key,
        ),
    )


# --------------------------------------------------------------------------- #
# count_club
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClubMember:
    """One historical peer (not the subject) who also clears a threshold."""

    label: str
    value: float
    year: int


def detect_count_club(
    *,
    subject: FactSubject,
    metric: str,
    cohort_key: Optional[str],
    subject_value: float,
    threshold: float,
    since_year: int,
    other_members: Sequence[ClubMember],
    higher_is_better: bool = True,
    baseline_version: Optional[str] = None,
) -> Optional[Fact]:
    """Count historical peers meeting a fixed condition.

    Mockup example: "8-rookie club since 2017". The subject's own
    qualification is checked here (not assumed) so
    callers can invoke this defensively; ``other_members`` is the
    caller-fetched roster of every OTHER historical peer (any year within
    the search window) who also clears ``threshold`` on ``metric``.
    Smaller/rarer clubs score more notable than large, common ones.

    Args:
        subject: Who the fact is about.
        metric: e.g. ``"gmsc"``.
        cohort_key: Informational cohort tag carried onto the Fact.
        subject_value: The subject's value on ``metric``.
        threshold: The qualifying bar.
        since_year: The start of the historical search window.
        other_members: Historical peers who also clear ``threshold``
            (subject excluded).
        higher_is_better: ``False`` for metrics where lower is better.
        baseline_version: Informational version tag.

    Returns:
        ``None`` when the subject doesn't actually clear ``threshold``,
        or ``other_members`` is empty (a "club of one" isn't a club Fact).
    """
    subject_qualifies = (
        subject_value >= threshold if higher_is_better else subject_value <= threshold
    )
    if not subject_qualifies or not other_members:
        return None

    count = len(other_members) + 1
    notability = _clamp01(1.2 - count / 20.0)

    return Fact(
        kind=FactKind.COUNT_CLUB,
        subject=subject,
        metric=metric,
        cohort=cohort_key,
        values={
            "value": subject_value,
            "threshold": threshold,
            "count": count,
            "since_year": since_year,
        },
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.COUNT_CLUB.value,
            baseline_version=baseline_version,
            cohort_key=cohort_key,
        ),
    )


# --------------------------------------------------------------------------- #
# first_since
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PriorHolder:
    """The most recent prior peer to hold a feat, before the subject."""

    label: str
    value: float
    year: int


def detect_first_since(
    *,
    subject: FactSubject,
    metric: str,
    cohort_key: Optional[str],
    subject_value: float,
    current_year: int,
    since_year: int,
    most_recent_prior: Optional[PriorHolder],
    baseline_version: Optional[str] = None,
) -> Fact:
    """The most-recent prior occurrence of a feat.

    Mockup example: "most by a #2 pick in an SL debut since 2019".

    Args:
        subject: Who the fact is about.
        metric: e.g. ``"gmsc"``.
        cohort_key: Informational cohort tag carried onto the Fact.
        subject_value: The subject's value on ``metric``.
        current_year: The year of the subject's instance.
        since_year: The start of the historical search window used when
            no prior instance exists at all (an all-time-in-window first).
        most_recent_prior: The last peer before the subject to clear the
            same bar, or ``None`` when nobody has since ``since_year``.
        baseline_version: Informational version tag.

    Returns:
        Always a :class:`Fact` -- a "first since" is inherently a
        superlative; a longer gap since the last occurrence only tunes
        how loudly it reads (``most_recent_prior is None``, or the prior
        instance being the same year as the subject's, is the notability
        floor -- 0.6).
    """
    prior_year = most_recent_prior.year if most_recent_prior else since_year
    gap_years = max(0, current_year - prior_year)
    notability = _clamp01(0.6 + gap_years * 0.08)

    values: dict[str, Any] = {
        "value": subject_value,
        "since_year": prior_year,
    }
    if most_recent_prior is not None:
        values["runner_up"] = {
            "who": most_recent_prior.label,
            "value": most_recent_prior.value,
        }

    return Fact(
        kind=FactKind.FIRST_SINCE,
        subject=subject,
        metric=metric,
        cohort=cohort_key,
        values=values,
        notability=notability,
        provenance=FactProvenance(
            detector_id=FactKind.FIRST_SINCE.value,
            baseline_version=baseline_version,
            cohort_key=cohort_key,
        ),
    )

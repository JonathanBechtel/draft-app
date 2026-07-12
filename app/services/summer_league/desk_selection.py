"""Stage 2 of the Summer League Desk commentary pipeline: notability & selection.

Stage 1 (`app.services.summer_league.desk_facts`, #520) detects and scores
:class:`~app.services.summer_league.desk_facts.Fact` records. Stage 3 (#519)
renders selected Facts to prose via a template registry. **This module picks
the angle -- it renders no prose** (`docs/plans/summer-league-scouts-desk-behavior-spec.md`
§11 Stage 2):

    collect all fired facts -> dedup overlapping angles -> drop anything below
    the notability floor -> sort by notability descending -> take the
    surface's top-k.

Two policies drive that pipeline, both pinned by spec §11 Stage 2:

* **Dedup / subsumption.** Subsumption is about *restatement*, not merely
  sharing an axis. Spec §11 Stage 2 pins exactly one relation: "dedup
  overlapping angles (rank=1 subsumes its own percentile)" -- a #1 cohort
  rank and its own percentile grade on the same metric/cohort restate one
  data point, so the rank makes the percentile redundant. But a `streak`
  and a `percentile` on that *same* axis say different things (an active
  run vs. an event-aggregate standing), so neither subsumes the other.
  :func:`dedup_facts` therefore does NOT collapse an axis to one survivor;
  it (a) collapses exact duplicates -- two Facts of the SAME kind on the
  same axis keep the more notable one -- and (b) applies an explicit,
  documented :data:`_SUBSUMPTION_RULES` relation over *pairs of FactKinds*,
  each gated on a strength condition (rank-1 cohort_rank subsumes
  percentile; a rank-7 cohort_rank subsumes nothing). Kinds with no
  declared relation both survive. See :data:`_SUBSUMPTION_RULES`.
* **Chips bypass selection.** "Grade chips render regardless (a chip is not
  prose)" -- chips read off the raw, undeduped, unfiltered Fact list
  (:func:`chip_facts`), never off :func:`select_facts`'s output. A Fact
  suppressed from prose (below the notability floor, or subsumed by a
  stronger overlapping Fact) is never deleted -- it simply isn't selected;
  the caller's original Fact list, and therefore its chip rendering, is
  unaffected.

**Not in scope here (deliberately):** template rendering (#519), persisting
selections onto T2/T4 `facts` JSONB columns (#519), and the spec's "prefer
fresh facts (changed since last tick)" tick-note/ledger-echo tiebreak -- that
needs tick-history data this module has no way to fetch without a database
(out of scope per this ticket's Rule 1). When #519 wires up persistence and
has a real "last selected at" signal per Fact, it can fold freshness into its
own pre-filtering or tiebreak before calling :func:`select_facts`; the
notability-first ordering here is unaffected either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from app.services.summer_league.desk_facts import Fact, FactKind

# Below this, a Fact isn't "worth a sentence" (spec §11 Stage 2) -- it may
# still back a grade chip (see module docstring), but prose selection drops
# it. 0.5 is the midpoint of each detector's 0..1 notability range: every
# detector in #520 scores extremity/superlatives (rank 1, ~95th+/5th-pctl,
# "first/only/most") near 1.0 and mid-pack/unremarkable outcomes near 0.0, so
# a floor at the midpoint reads as "at least moderately extreme," matching
# the spec's "mid-pack (~50th pctl) scores low" framing.
NOTABILITY_FLOOR = 0.5


class Surface(str, Enum):
    """The prose surfaces Stage 2 selects for (spec §11 Stage 2, §4-§5)."""

    HERO_TAGLINE = "hero_tagline"
    TICK_NOTE = "tick_note"
    LEDGER_ECHO = "ledger_echo"


# Per-surface top-k (spec §11 Stage 2: "hero tagline k=1; tick notes / Ledger
# echoes k=few"). Named, module-level, and overridable per call via
# `select_facts(..., k=...)` -- never inlined as a magic number inside the
# selection function itself. Tick notes and Ledger echoes are pinned to the
# same "few" since the spec groups them under one policy; tune independently
# here (or override per-call) if product wants them to diverge later.
SURFACE_K: dict[Surface, int] = {
    Surface.HERO_TAGLINE: 1,
    Surface.TICK_NOTE: 3,
    Surface.LEDGER_ECHO: 3,
}

# Sentinel for the (rare, comparable) missing `competition_id` in the sort
# key below -- real ids are positive Postgres serials, so -1 always sorts
# before any real event and never collides with one.
_NO_COMPETITION = -1


def _dedup_axis(fact: Fact) -> tuple[int, Optional[int], str, Optional[str]]:
    """The (subject, metric, cohort) axis two Facts must share to overlap."""
    return (
        fact.subject.player_id,
        fact.subject.competition_id,
        fact.metric,
        fact.cohort,
    )


def _sort_key(fact: Fact) -> tuple[float, str, str, str, int, int]:
    """A total order over Facts: notability descending, then a stable tiebreak.

    Ties on notability are expected (several detectors saturate at 1.0 for a
    clean superlative), so relying on insertion order or a dict/set's
    iteration order would make selection non-deterministic across runs given
    differently-ordered input. Every field that could plausibly tie is
    folded into the key, ending in ``player_id``/``competition_id`` -- the
    one pair guaranteed unique per subject -- so two distinct Facts never
    compare equal under this key.
    """
    return (
        -fact.notability,
        fact.kind.value,
        fact.metric,
        fact.cohort or "",
        fact.subject.player_id,
        fact.subject.competition_id
        if fact.subject.competition_id is not None
        else _NO_COMPETITION,
    )


@dataclass(frozen=True)
class _SubsumptionRule:
    """A "``strong`` restates and outranks ``weak``" relation between two kinds.

    Applies only to two Facts already on the SAME (subject, metric, cohort)
    axis, and only when ``condition`` holds on the stronger Fact -- so a
    weak-but-present strong-kind Fact (e.g. a cohort_rank of 7) subsumes
    nothing. ``condition`` reads the stronger Fact's own ``values``; it must
    NOT assume notability ordering implies the relation (a rank-7 fact can
    still out-notability a mid-pack percentile without subsuming it).
    """

    strong: FactKind
    weak: FactKind
    condition: Callable[[Fact], bool]


def _is_rank_one(fact: Fact) -> bool:
    """Whether a ``cohort_rank`` Fact is the #1 (superlative) case.

    ``detect_cohort_rank`` stores the computed rank in ``values["rank"]``
    (1 == leads the cohort). Only rank 1 restates "top of the cohort" the
    way a top-percentile grade does; any other rank is a distinct, weaker
    standing that does not subsume the percentile.
    """
    return fact.values.get("rank") == 1


# The subsumption relation, derived from spec §11 Stage 2's ONE pinned
# example -- "dedup overlapping angles (rank=1 subsumes its own percentile)"
# (docs/plans/summer-league-scouts-desk-behavior-spec.md line 432). The spec
# pins no other pair, so this list is deliberately minimal: a rank-1
# `cohort_rank` subsumes the `percentile` restating the same standing. Add a
# row here (with its strength condition) if a future spec revision pins
# another restatement pair; the default for any undeclared pair is "no
# subsumption -- both survive." (`leads_field` is itself a rank-1 superlative
# but always carries a `field:{label}` cohort, so it never shares an axis
# with a slot/status-cohort `percentile`/`cohort_rank` and needs no rule.)
_SUBSUMPTION_RULES: tuple[_SubsumptionRule, ...] = (
    _SubsumptionRule(
        strong=FactKind.COHORT_RANK,
        weak=FactKind.PERCENTILE,
        condition=_is_rank_one,
    ),
)


def _dedup_within_axis(group: Sequence[Fact]) -> list[Fact]:
    """Collapse one axis-group: exact-duplicate kinds, then subsumption.

    Step 1 keeps the single most-notable Fact per :class:`FactKind` (two
    percentile Facts on one axis are the same claim twice). Step 2 removes
    a surviving weak-kind Fact when a surviving strong-kind Fact subsumes it
    per :data:`_SUBSUMPTION_RULES`. Kinds with no declared relation both
    survive -- a streak and a percentile on the same axis are different
    claims, so both stay.
    """
    best_per_kind: dict[FactKind, Fact] = {}
    for fact in group:
        current = best_per_kind.get(fact.kind)
        if current is None or _sort_key(fact) < _sort_key(current):
            best_per_kind[fact.kind] = fact

    subsumed: set[FactKind] = set()
    for rule in _SUBSUMPTION_RULES:
        strong = best_per_kind.get(rule.strong)
        if strong is not None and rule.weak in best_per_kind and rule.condition(strong):
            subsumed.add(rule.weak)

    return [f for kind, f in best_per_kind.items() if kind not in subsumed]


def dedup_facts(facts: Sequence[Fact]) -> list[Fact]:
    """Remove restated/duplicate Facts, keeping genuinely distinct angles.

    NOT an axis-wide collapse. Two Facts share an axis (same
    ``subject.player_id``, ``subject.competition_id``, ``metric``, ``cohort``)
    only get merged when one actually restates the other:

    * **Exact duplicate** -- two Facts of the SAME kind on one axis: keep the
      more notable one (ties broken by :func:`_sort_key`).
    * **Subsumption** -- a stronger kind restates a weaker one per
      :data:`_SUBSUMPTION_RULES`, and its strength condition holds (a rank-1
      ``cohort_rank`` subsumes ``percentile``; a rank-7 one subsumes
      nothing).

    Everything else survives: a ``streak`` and a ``percentile`` on the same
    metric+cohort say different things, so BOTH stay -- which is what lets a
    single-subject ``TICK_NOTE``/``LEDGER_ECHO`` surface actually reach its
    ``k>1`` budget. Facts on different metrics, cohorts, or subjects never
    collide.

    This never mutates or drops anything from the caller's original Fact list
    -- it returns a new, smaller list. Chip rendering must keep using the
    original (or :func:`chip_facts`), never this function's output.

    Args:
        facts: Facts fired for one rendering pass (any subjects/metrics/
            competitions mixed together).

    Returns:
        The surviving Facts, sorted strongest-first by :func:`_sort_key`.
        Empty when ``facts`` is empty.
    """
    groups: dict[tuple[int, Optional[int], str, Optional[str]], list[Fact]] = {}
    for fact in facts:
        groups.setdefault(_dedup_axis(fact), []).append(fact)

    survivors: list[Fact] = []
    for group in groups.values():
        survivors.extend(_dedup_within_axis(group))
    survivors.sort(key=_sort_key)
    return survivors


def meets_notability_floor(fact: Fact, *, floor: float = NOTABILITY_FLOOR) -> bool:
    """Whether ``fact`` alone clears the prose notability floor.

    Exposed standalone (not just inlined in :func:`select_facts`) so a
    caller that needs a per-fact yes/no -- e.g. #519 deciding whether to
    render one specific fact's text outside the top-k surfaces -- doesn't
    have to reimplement the comparison or reach into :data:`NOTABILITY_FLOOR`
    directly.

    Args:
        fact: The Fact to check.
        floor: Override for :data:`NOTABILITY_FLOOR`.

    Returns:
        ``True`` when ``fact.notability >= floor``.
    """
    return fact.notability >= floor


def chip_facts(facts: Sequence[Fact]) -> list[Fact]:
    """Chips bypass selection entirely (spec §11 Stage 2: "chips render regardless").

    A deliberate, named identity function -- not a shortcut to "just use the
    raw list" -- so a chip-rendering call site in #519 reads as an explicit
    policy choice ("call ``chip_facts``, not ``select_facts``") rather than
    the reader having to infer it from surrounding code. Chips never dedup
    or floor-filter: a gated/low-notability percentile still renders as a
    grade chip even though it would never make prose.

    Args:
        facts: All Facts fired for the subject; chips render every one.

    Returns:
        A new list with the same Facts, in input order -- never the
        caller's original list object, so downstream chip code can't
        accidentally mutate the Stage 1 Fact list.
    """
    return list(facts)


def select_facts(
    facts: Sequence[Fact],
    *,
    surface: Surface,
    k: Optional[int] = None,
    notability_floor: float = NOTABILITY_FLOOR,
) -> list[Fact]:
    """Stage 2: dedup, floor, rank, and cap Facts for one prose surface.

    Implements the full spec §11 Stage 2 pipeline: dedup overlapping angles
    (:func:`dedup_facts`) -> drop anything below ``notability_floor``
    (:func:`meets_notability_floor`) -> sort by notability descending,
    extremity first (so "best #1-pick start ever" beats "96th pctl" beats a
    mid-pack "three-time 20/65%") -> take the surface's top-k.

    Never mutates or drops anything from the ``facts`` argument -- this
    returns a new, smaller list for one prose surface. Grade chips must keep
    rendering off the full Fact list (:func:`chip_facts`) regardless of what
    this returns.

    Args:
        facts: All Facts fired for the subject(s) this surface is rendering.
        surface: Which prose surface is selecting -- looks up that
            surface's ``k`` from :data:`SURFACE_K` unless ``k`` overrides
            it.
        k: Explicit cap, overriding the surface default. Must be >= 1; pass
            this for a one-off caller instead of editing :data:`SURFACE_K`.
        notability_floor: Facts scoring below this are suppressed from
            prose. They remain in the caller's original ``facts`` (and thus
            still available to :func:`chip_facts`) -- this function only
            decides what one prose surface shows.

    Returns:
        Up to ``k`` Facts, strongest first, deterministically ordered.
        Empty when nothing fired, or everything fired below the floor.

    Raises:
        ValueError: ``k`` (explicit or resolved from ``surface``) is < 1.
    """
    effective_k = k if k is not None else SURFACE_K[surface]
    if effective_k < 1:
        raise ValueError(f"k must be >= 1, got {effective_k}")

    deduped = dedup_facts(facts)
    eligible = [
        fact for fact in deduped if meets_notability_floor(fact, floor=notability_floor)
    ]
    eligible.sort(key=_sort_key)
    return eligible[:effective_k]

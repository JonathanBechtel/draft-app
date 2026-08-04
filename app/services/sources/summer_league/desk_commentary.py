"""Stage 3 of the Summer League Desk commentary pipeline: realization + persistence.

Stage 1 (`app.services.sources.summer_league.desk_facts`, #520) detects and scores
:class:`~app.services.sources.summer_league.desk_facts.Fact` records. Stage 2
(`app.services.sources.summer_league.desk_selection`, #518) picks the angle: dedup,
notability floor, top-k per surface. **This module renders the chosen Fact
to a string and persists it** (`docs/plans/summer-league-scouts-desk-behavior-spec.md`
§11 Stage 3):

    a template registry keyed by `fact.kind` (+ a value-driven variant
    family, e.g. `cohort_rank` splits on `rank == 1`) -> 2 curated phrasings
    per family, picked by a **stable hash of (subject.player_id, fact.kind)**
    -> the filled string.

**Deterministic, not random.** No `random`, no clock, no runtime LLM call --
:func:`_stable_variant_index` is a pure SHA-256 hash of the subject id and
Fact kind, so the same Fact always renders the same string (this is exactly
what the double-render byte-equality tests verify). Per spec: "LLM only
offline: if richer phrasings are wanted later, an LLM *authors the template
library* once (human-reviewed); runtime only fills slots -- never generates."

**One Fact -> three read-offs, never disagreeing.** Per spec §11 Stage 3
("Multi-surface realizers: one Fact -> prose (headline), a chip (percentile
-> grade chip), or a share card"), this module exposes exactly two renderer
families -- :func:`render_fact` (prose sentence) and :func:`render_chip`
(short badge text) -- both pure functions of one ``Fact``. There is no
separate "share" template family: a hero's rendered prose (via
:data:`SLATE_HERO_PROSE_SURFACES`) already IS the share-ready headline/
tagline text (`app.services.event_desk.payload.DeskHero.tagline`), and chips
already are the share-card grade badges. Reusing the same renderer pool for
every surface is what makes "chips and prose can therefore never disagree"
true by construction rather than by convention.

**Gated grades: hedge, don't refuse.** A :attr:`~app.services.sources.summer_league.desk_grades.GradeRow.gated`
percentile already can't clear the prose notability floor under the normal
Stage 1/2 pipeline (`desk_facts.GATED_NOTABILITY_DAMPING` caps its score
below `desk_selection.NOTABILITY_FLOOR`), so it is never *selected* for
prose. :func:`_render_percentile` still carries an explicit gated branch as
defense-in-depth for a caller that renders a gated Fact directly, bypassing
selection -- it hedges ("early read ... sample is still thin") rather than
reading as a confident superlative. The **chip renders regardless** (spec:
"grade chips render regardless (a chip is not prose)") -- :func:`_chip_percentile`
prefixes a gated chip with an honest ``"early · "`` qualifier instead of
suppressing it.

**Cohort keys never leak into copy.** T1's `cohort_key` is a machine key
(`slot:1-4`, `round:1_late`, `round:2`, `status:undrafted`, plus `debut:`-
prefixed mirrors -- see `app.services.sources.summer_league.cohort_baselines`
module docstring for the exact grammar). :func:`humanize_cohort_key`
translates every one of those into a human noun phrase (e.g. `slot:1-4` ->
"top-4 cohort") before it ever reaches a template; no renderer in this
module interpolates a raw `cohort_key` string.

**Editorial constraints (behavior spec §3, §8).** No roster/contract/signing
claims, no competitive/tournament framing, no unattributed superlatives the
data can't support ("career-best" -- this app only has Summer League
history, never a player's full career). See
`tests/unit/test_sl_desk_commentary.py::test_rendered_output_matrix_has_no_banned_terms`
for the scan over rendered (not just literal source) output.

**Persistence (spec §11 "Where it runs / storage").** :func:`persist_grade_facts`
and :func:`persist_slate_facts` build the `facts` JSONB payload
(:func:`build_facts_payload`: each selected/chip-eligible Fact's
:meth:`~app.services.sources.summer_league.desk_facts.Fact.to_dict` plus its
rendered ``chip``/``prose`` strings and which prose surfaces selected it)
and write it onto the existing T2 (`summer_league_desk_player_grades`) /
T4 (`summer_league_desk_slate`) row -- Job B (#521, hourly tick) calls these
after Stage 1/2 have produced the Facts for a player/game. "Hero refs where
applicable" (ticket #519) means: :func:`persist_slate_facts`'s ``is_hero``
flag adds `Surface.HERO_TAGLINE` to the prose surfaces considered for that
game's Facts, so a hero row's `facts` payload carries the same headline/
tagline text the framework's `event_desk_state.hero_ref` (owned by the read
service, #516/#508) ultimately points at -- there is no second, hero-only
rendering path to drift out of sync with the tick-note prose.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_desk import (
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
)
from app.services.sources.summer_league.desk_facts import Fact, FactKind
from app.services.sources.summer_league.desk_selection import Surface, select_facts


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _num(x: Any) -> str:
    """Compact number formatting: whole numbers with no decimal, else 1dp."""
    v = float(x)
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


def _ordinal(n: Any) -> str:
    """``96`` -> ``"96th"``; ``1`` -> ``"1st"``; ``3`` -> ``"3rd"``."""
    i = int(round(float(n)))
    if 10 <= abs(i) % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(abs(i) % 10, "th")
    return f"{i}{suffix}"


# Non-exhaustive labels for the metrics V1 detectors actually emit; anything
# not listed falls back to a de-slugged version of the raw metric string
# (e.g. "efg_pct" -> "efg pct") rather than raising.
_METRIC_LABELS: dict[str, str] = {
    "gmsc": "Game Score",
    "pts": "points",
    "reb": "rebounds",
    "ast": "assists",
    "stl": "steals",
    "blk": "blocks",
    "tov": "turnovers",
    "tov_pct": "turnover rate",
    "ts_pct": "true shooting",
    "efg_pct": "effective FG%",
    "ast_pct": "assist rate",
    "usg_pct": "usage rate",
    "reb_pct": "rebound rate",
    "fg3ar": "3PT rate",
    "ftr": "free throw rate",
}


def metric_label(metric: str) -> str:
    """Human-facing label for a Fact's ``metric`` field."""
    return _METRIC_LABELS.get(metric, metric.replace("_", " "))


def _humanize_cohort_suffix(suffix: str) -> str:
    """Translate a cohort_key's suffix (post-prefix) into a noun phrase.

    Shared between the ``slot:``/``round:``/``status:`` (event-grain) and
    ``debut:`` prefixes since #502's ``cohort_key_for`` mirrors the exact
    same suffix grammar under both (module docstring,
    `app/services/summer_league/cohort_baselines.py`).
    """
    if suffix == "undrafted":
        return "undrafted cohort"
    if suffix == "1_late":
        return "late first-round cohort"
    if suffix == "2":
        return "second-round cohort"
    if "-" in suffix:
        low_s, high_s = suffix.split("-", 1)
        try:
            low, high = int(low_s), int(high_s)
        except ValueError:
            return "his cohort"
        if low <= 1:
            return f"top-{high} cohort"
        return f"picks {low}-{high} cohort"
    return "his cohort"


def humanize_cohort_key(cohort_key: Optional[str]) -> str:
    """Translate a raw T1 ``cohort_key`` into user-facing copy.

    The hard contract (ticket #519): ``slot:{low}-{high}``, ``round:1_late``,
    ``round:2``, ``status:undrafted``, plus ``debut:``-prefixed mirrors, and
    a live-field ``field:{label}`` form (`desk_facts.detect_leads_field`).
    Raw keys must never leak into rendered copy.

    Args:
        cohort_key: A T1-style cohort key, a ``field:{label}`` live-field
            key, or ``None`` (no cohort context).

    Returns:
        A noun phrase safe to drop into ``"the {phrase}"`` in a template
        (e.g. ``"top-4 cohort"``, ``"undrafted cohort"``, or -- for a
        ``field:`` key -- the caller-supplied label verbatim, e.g.
        ``"rookies tonight"``). ``"his cohort"`` when nothing usable is
        available.
    """
    if not cohort_key:
        return "his cohort"
    prefix, sep, suffix = cohort_key.partition(":")
    if not sep:
        return "his cohort"
    if prefix == "field":
        return suffix
    return _humanize_cohort_suffix(suffix)


# --------------------------------------------------------------------------- #
# Deterministic variant selection (spec §11 Stage 3: "Variety without
# randomness")
# --------------------------------------------------------------------------- #
def _stable_variant_index(fact: Fact, n_variants: int) -> int:
    """A deterministic ``0..n_variants-1`` index, stable per (subject, kind).

    Pure SHA-256 hash of ``subject.player_id`` + ``fact.kind`` (spec §11
    Stage 3: "picked by a stable key (hash of subject)") -- no ``random``,
    no clock, no I/O. The same Fact (same subject, same kind) always
    resolves to the same variant index, which is what makes double-rendering
    a Fact byte-identical regardless of call order.

    Args:
        fact: The Fact choosing a phrasing variant.
        n_variants: How many curated variants the calling family offers.

    Returns:
        A stable index in ``[0, n_variants)``. Always ``0`` when
        ``n_variants <= 1`` (nothing to vary).
    """
    if n_variants <= 1:
        return 0
    key = f"{fact.subject.player_id}:{fact.kind.value}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:8], "big") % n_variants


def _pick(fact: Fact, variants: Sequence[Callable[[], str]]) -> str:
    """Resolve one of ``variants`` via :func:`_stable_variant_index` and call it."""
    return variants[_stable_variant_index(fact, len(variants))]()


# --------------------------------------------------------------------------- #
# Prose renderers -- one per FactKind, keyed by fact.kind (spec §11 Stage 3)
# --------------------------------------------------------------------------- #
def _render_cohort_rank(fact: Fact) -> str:
    value = _num(fact.values["value"])
    metric = metric_label(fact.metric)
    cohort = humanize_cohort_key(fact.cohort)
    runner_up = fact.values.get("runner_up") or {}
    ru_who = runner_up.get("who", "the field")
    ru_val = _num(runner_up.get("value", 0))
    rank = int(fact.values.get("rank", 1))
    of = int(fact.values.get("of", 1))

    if rank == 1:
        variants: tuple[Callable[[], str], ...] = (
            lambda: (
                f"His {value} {metric} is the best mark in the {cohort} to date "
                f"— ahead of {ru_who} ({ru_val})."
            ),
            lambda: (
                f"Nobody in the {cohort} has topped his {value} {metric}; "
                f"{ru_who} ({ru_val}) is next closest."
            ),
        )
    else:
        rank_ord = _ordinal(rank)
        variants = (
            lambda: (
                f"His {value} {metric} ranks {rank_ord} of {of} in the {cohort}, "
                f"trailing {ru_who} ({ru_val})."
            ),
            lambda: (
                f"Among the {cohort}, he sits {rank_ord} of {of} at {value} {metric} "
                f"— {ru_who} ({ru_val}) leads the group."
            ),
        )
    return _pick(fact, variants)


def _render_percentile(fact: Fact) -> str:
    value = _num(fact.values["value"])
    pctl_ord = _ordinal(fact.values["pctl"])
    cohort = humanize_cohort_key(fact.cohort)
    metric = metric_label(fact.metric)
    gated = bool(fact.values.get("gated", False))

    if gated:
        # Defense-in-depth: under the normal Stage 1/2 pipeline a gated
        # percentile's notability is dampened below the prose floor
        # (desk_facts.GATED_NOTABILITY_DAMPING x desk_selection.NOTABILITY_FLOOR),
        # so `select_facts` never actually selects one for a prose surface.
        # This branch only fires if a caller renders a gated Fact directly.
        return (
            f"Early read: his {value} {metric} traces to the {pctl_ord} percentile "
            f"of the {cohort}, but the sample is still thin."
        )

    variants: tuple[Callable[[], str], ...] = (
        lambda: f"He's grading at the {pctl_ord} percentile of the {cohort} on {metric} ({value}).",
        lambda: f"{value} {metric} puts him in the {pctl_ord} percentile of the {cohort}.",
    )
    return _pick(fact, variants)


def _render_streak(fact: Fact) -> str:
    length = int(fact.values["length"])
    min_value = _num(fact.values["min_value"])
    avg_value = _num(fact.values["avg_value"])
    avg_pctl_ord = _ordinal(fact.values["avg_pctl"])
    cohort = humanize_cohort_key(fact.cohort)
    metric = metric_label(fact.metric)

    variants: tuple[Callable[[], str], ...] = (
        lambda: (
            f"{length}-game streak at {min_value}+ {metric}, averaging the "
            f"{avg_pctl_ord} percentile of the {cohort}."
        ),
        lambda: (
            f"He's strung together {length} straight games at {min_value}+ {metric} "
            f"— a {avg_pctl_ord}-percentile average ({avg_value}) over the run "
            f"within the {cohort}."
        ),
    )
    return _pick(fact, variants)


def _render_self_delta(fact: Fact) -> str:
    gp = int(fact.values["gp"])
    delta = float(fact.values["delta"])
    since_year = int(fact.values["since_year"])
    prior_value = _num(fact.values["prior_value"])
    metric = metric_label(fact.metric)

    if delta >= 0:
        variants: tuple[Callable[[], str], ...] = (
            lambda: (
                f"+{_num(delta)} {metric} through {gp} games versus his "
                f"{since_year} Summer League ({prior_value})."
            ),
            lambda: (
                f"He's running {_num(delta)} {metric} ahead of his {since_year} "
                f"summer through {gp} games."
            ),
        )
    else:
        variants = (
            lambda: (
                f"{_num(delta)} {metric} through {gp} games versus his "
                f"{since_year} Summer League ({prior_value})."
            ),
            lambda: (
                f"He's tracking {_num(abs(delta))} {metric} below his {since_year} "
                f"summer through {gp} games."
            ),
        )
    return _pick(fact, variants)


def _render_leads_field(fact: Fact) -> str:
    value = _num(fact.values["value"])
    metric = metric_label(fact.metric)
    cohort = humanize_cohort_key(fact.cohort)
    runner_up = fact.values.get("runner_up") or {}
    ru_who = runner_up.get("who", "the field")
    ru_val = _num(runner_up.get("value", 0))

    variants: tuple[Callable[[], str], ...] = (
        lambda: f"Leads all {cohort} tonight at {value} {metric} — {ru_who} ({ru_val}) is next closest.",
        lambda: f"Nobody among {cohort} has matched his {value} {metric} tonight; {ru_who} trails at {ru_val}.",
    )
    return _pick(fact, variants)


def _render_debut_vs_bar(fact: Fact) -> str:
    value = _num(fact.values["value"])
    bar = _num(fact.values["bar"])
    delta = float(fact.values["delta"])
    cohort = humanize_cohort_key(fact.cohort)
    metric = metric_label(fact.metric)

    if delta >= 0:
        variants: tuple[Callable[[], str], ...] = (
            lambda: f"His {value} {metric} debut clears the {cohort} bar of {bar} by {_num(delta)}.",
            lambda: (
                f"First Summer League floor: {value} {metric}, {_num(delta)} above "
                f"the {cohort}'s historical debut bar ({bar})."
            ),
        )
    else:
        variants = (
            lambda: f"His {value} {metric} debut sits {_num(abs(delta))} below the {cohort} bar of {bar}.",
            lambda: (
                f"First Summer League floor: {value} {metric}, {_num(abs(delta))} "
                f"shy of the {cohort}'s historical debut bar ({bar})."
            ),
        )
    return _pick(fact, variants)


def _render_count_club(fact: Fact) -> str:
    value = _num(fact.values["value"])
    threshold = _num(fact.values["threshold"])
    count = int(fact.values["count"])
    since_year = int(fact.values["since_year"])
    metric = metric_label(fact.metric)

    variants: tuple[Callable[[], str], ...] = (
        lambda: (
            f"{count}-player club since {since_year} at {threshold}+ {metric} "
            f"— he's the latest to clear it at {value}."
        ),
        lambda: (
            f"Only {count} players have hit {threshold}+ {metric} since {since_year}; "
            f"he's now one of them at {value}."
        ),
    )
    return _pick(fact, variants)


def _render_first_since(fact: Fact) -> str:
    value = _num(fact.values["value"])
    since_year = int(fact.values["since_year"])
    metric = metric_label(fact.metric)
    runner_up = fact.values.get("runner_up")

    if runner_up:
        ru_who = runner_up.get("who", "the last holder")
        ru_val = _num(runner_up.get("value", 0))
        variants: tuple[Callable[[], str], ...] = (
            lambda: f"His {value} {metric} is the most since {since_year}, when {ru_who} posted {ru_val}.",
            lambda: f"Nobody has reached {value} {metric} since {ru_who} in {since_year}.",
        )
    else:
        variants = (
            lambda: f"His {value} {metric} is the best mark since at least {since_year}.",
            lambda: f"Nothing since {since_year} has matched his {value} {metric}.",
        )
    return _pick(fact, variants)


_PROSE_RENDERERS: dict[FactKind, Callable[[Fact], str]] = {
    FactKind.COHORT_RANK: _render_cohort_rank,
    FactKind.PERCENTILE: _render_percentile,
    FactKind.STREAK: _render_streak,
    FactKind.SELF_DELTA: _render_self_delta,
    FactKind.LEADS_FIELD: _render_leads_field,
    FactKind.DEBUT_VS_BAR: _render_debut_vs_bar,
    FactKind.COUNT_CLUB: _render_count_club,
    FactKind.FIRST_SINCE: _render_first_since,
}


def render_fact(fact: Fact) -> str:
    """Realize one Fact into a prose sentence (spec §11 Stage 3).

    Pure and deterministic: the same ``fact`` always renders the same
    string (see :func:`_stable_variant_index`). This is the single
    prose-realization entry point every surface (hero tagline, tick note,
    Ledger echo, share caption) reuses -- there is no separate per-surface
    template family.

    Args:
        fact: The Fact to render.

    Returns:
        The rendered sentence.
    """
    return _PROSE_RENDERERS[fact.kind](fact)


# --------------------------------------------------------------------------- #
# Chip renderers -- short badge text; chips bypass Stage 2 selection
# (`desk_selection.chip_facts`) and render for EVERY fired Fact, gated or not.
# --------------------------------------------------------------------------- #
def _chip_cohort_rank(fact: Fact) -> str:
    rank = int(fact.values.get("rank", 1))
    of = int(fact.values.get("of", 1))
    cohort = humanize_cohort_key(fact.cohort)
    label = "#1" if rank == 1 else _ordinal(rank)
    return f"{label} of {of} · {cohort}"


def _chip_percentile(fact: Fact) -> str:
    pctl_ord = _ordinal(fact.values["pctl"])
    cohort = humanize_cohort_key(fact.cohort)
    prefix = "early · " if fact.values.get("gated", False) else ""
    return f"{prefix}{pctl_ord} pctl · {cohort}"


def _chip_streak(fact: Fact) -> str:
    length = int(fact.values["length"])
    min_value = _num(fact.values["min_value"])
    metric = metric_label(fact.metric)
    return f"{length}-game streak · {min_value}+ {metric}"


def _chip_self_delta(fact: Fact) -> str:
    delta = float(fact.values["delta"])
    since_year = int(fact.values["since_year"])
    metric = metric_label(fact.metric)
    sign = "+" if delta >= 0 else ""
    return f"{sign}{_num(delta)} {metric} vs {since_year}"


def _chip_leads_field(fact: Fact) -> str:
    value = _num(fact.values["value"])
    metric = metric_label(fact.metric)
    cohort = humanize_cohort_key(fact.cohort)
    return f"Leads {cohort} · {value} {metric}"


def _chip_debut_vs_bar(fact: Fact) -> str:
    value = _num(fact.values["value"])
    bar = _num(fact.values["bar"])
    metric = metric_label(fact.metric)
    return f"Debut · {value} vs {bar} {metric} bar"


def _chip_count_club(fact: Fact) -> str:
    count = int(fact.values["count"])
    threshold = _num(fact.values["threshold"])
    metric = metric_label(fact.metric)
    return f"{count}-player club · {threshold}+ {metric}"


def _chip_first_since(fact: Fact) -> str:
    since_year = int(fact.values["since_year"])
    value = _num(fact.values["value"])
    metric = metric_label(fact.metric)
    return f"Most since {since_year} · {value} {metric}"


_CHIP_RENDERERS: dict[FactKind, Callable[[Fact], str]] = {
    FactKind.COHORT_RANK: _chip_cohort_rank,
    FactKind.PERCENTILE: _chip_percentile,
    FactKind.STREAK: _chip_streak,
    FactKind.SELF_DELTA: _chip_self_delta,
    FactKind.LEADS_FIELD: _chip_leads_field,
    FactKind.DEBUT_VS_BAR: _chip_debut_vs_bar,
    FactKind.COUNT_CLUB: _chip_count_club,
    FactKind.FIRST_SINCE: _chip_first_since,
}


def render_chip(fact: Fact) -> str:
    """Realize one Fact into short chip/badge text (spec §11 Stage 3).

    Chips render regardless of notability/gating -- see module docstring
    and `desk_selection.chip_facts`. A gated ``percentile`` Fact still gets
    a chip, prefixed ``"early · "`` rather than suppressed.

    Args:
        fact: The Fact to render.

    Returns:
        The rendered chip text.
    """
    return _CHIP_RENDERERS[fact.kind](fact)


def render_prose_for_surface(
    facts: Sequence[Fact], *, surface: Surface, k: Optional[int] = None
) -> list[str]:
    """Select (Stage 2) then render (Stage 3) a prose surface's Facts.

    Args:
        facts: All Facts fired for the subject(s) this surface renders.
        surface: Which prose surface is selecting (`desk_selection.Surface`).
        k: Explicit cap overriding the surface's default `SURFACE_K`.

    Returns:
        Up to ``k`` rendered sentences, strongest-first, deterministically
        ordered (mirrors `desk_selection.select_facts`'s ordering exactly).
    """
    return [render_fact(f) for f in select_facts(facts, surface=surface, k=k)]


# --------------------------------------------------------------------------- #
# Persistence -- T2 (summer_league_desk_player_grades) / T4 (summer_league_desk_slate)
# `facts` JSONB columns (spec §10 T2/T4, §11 "Where it runs / storage")
# --------------------------------------------------------------------------- #
# Which prose surfaces a T2 (per-player) vs T4 (per-game) row's Facts are
# checked against when building the persisted payload. T2 backs player-
# centric reads (tick notes, Ledger echoes); T4 backs the game-centric slate
# read plus -- only when the row `is_hero` -- the hero tagline (ticket #519
# "hero refs where applicable").
GRADE_PROSE_SURFACES: tuple[Surface, ...] = (Surface.TICK_NOTE, Surface.LEDGER_ECHO)
SLATE_PROSE_SURFACES: tuple[Surface, ...] = (Surface.TICK_NOTE,)
SLATE_HERO_PROSE_SURFACES: tuple[Surface, ...] = (
    Surface.TICK_NOTE,
    Surface.HERO_TAGLINE,
)


def build_facts_payload(
    facts: Sequence[Fact], *, prose_surfaces: Sequence[Surface]
) -> list[dict[str, Any]]:
    """Build the JSON-clean payload persisted onto a T2/T4 ``facts`` column.

    Every fired Fact gets one entry (chips render regardless of selection --
    module docstring); an entry's ``"prose"`` is populated only when that
    Fact was actually selected (Stage 2) for at least one of
    ``prose_surfaces``, and ``"selected_for"`` names which ones. Matches
    Facts back to their selection outcome by object identity (``id()``),
    not equality -- :class:`~app.services.sources.summer_league.desk_facts.Fact` is
    unhashable (its ``values`` field is a plain ``dict``) and two distinct
    Facts can be field-for-field equal without being the same fired
    instance.

    Args:
        facts: Every Fact fired for this row's subject (player or game).
        prose_surfaces: Which `desk_selection.Surface` values to run Stage 2
            selection against; a Fact selected by any of them gets rendered
            prose.

    Returns:
        One dict per input Fact, in input order: `Fact.to_dict()` plus
        ``"chip"`` (str), ``"prose"`` (str or ``None``), and
        ``"selected_for"`` (sorted list of surface-value strings, possibly
        empty).
    """
    selected_surfaces_by_id: dict[int, set[str]] = defaultdict(set)
    for surface in prose_surfaces:
        for selected in select_facts(facts, surface=surface):
            selected_surfaces_by_id[id(selected)].add(surface.value)

    payload: list[dict[str, Any]] = []
    for fact in facts:
        surfaces = sorted(selected_surfaces_by_id.get(id(fact), set()))
        entry = fact.to_dict()
        entry["chip"] = render_chip(fact)
        entry["prose"] = render_fact(fact) if surfaces else None
        entry["selected_for"] = surfaces
        payload.append(entry)
    return payload


async def persist_grade_facts(
    session: AsyncSession,
    *,
    player_id: int,
    competition_id: int,
    baseline_version: str,
    facts: Sequence[Fact],
) -> list[dict[str, Any]]:
    """Render + write ``facts`` onto an existing T2 grade row.

    Does not create the T2 row -- `desk_grades.grade_player_event` (Job B
    step 2) must have already written it; this only updates its ``facts``
    JSONB column. Does not commit; the caller controls the transaction
    (matches `grade_player_event` / `compute_desk_storylines`).

    Args:
        session: Active database session.
        player_id: The player the T2 row belongs to.
        competition_id: The event the T2 row belongs to.
        baseline_version: Which T1 baseline version the T2 row was graded
            against (part of T2's unique key).
        facts: Every Fact fired for this player this tick.

    Returns:
        The persisted payload (see :func:`build_facts_payload`).

    Raises:
        ValueError: No matching T2 row exists yet.
    """
    payload = build_facts_payload(facts, prose_surfaces=GRADE_PROSE_SURFACES)
    stmt = select(SummerLeagueDeskPlayerGrade).where(
        SummerLeagueDeskPlayerGrade.player_id == player_id,  # type: ignore[arg-type]
        SummerLeagueDeskPlayerGrade.competition_id == competition_id,  # type: ignore[arg-type]
        SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise ValueError(
            "No summer_league_desk_player_grades row for "
            f"player_id={player_id}, competition_id={competition_id}, "
            f"baseline_version={baseline_version!r} -- run grade_player_event "
            "(Job B step 2) before persisting commentary."
        )
    row.facts = payload
    session.add(row)
    await session.flush()
    return payload


async def persist_slate_facts(
    session: AsyncSession,
    *,
    game_id: int,
    facts: Sequence[Fact],
    is_hero: bool = False,
) -> list[dict[str, Any]]:
    """Render + write ``facts`` onto an existing T4 slate row.

    Does not create the T4 row -- `desk_storylines.compute_desk_storylines`
    (Job B step 3) must have already upserted it; this only updates its
    ``facts`` JSONB column. Does not commit; the caller controls the
    transaction.

    Args:
        session: Active database session.
        game_id: The game the T4 row belongs to (T4's unique key).
        facts: Every Fact fired for this game's tracked subjects this tick.
        is_hero: Whether this row is the day's hero game (`SummerLeagueDeskSlate.is_hero`).
            When true, Facts are also selected for `Surface.HERO_TAGLINE`
            (ticket #519 "hero refs where applicable") so the same payload
            backs both the slate row's tick-note read and the hero tagline.

    Returns:
        The persisted payload (see :func:`build_facts_payload`).

    Raises:
        ValueError: No matching T4 row exists yet.
    """
    prose_surfaces = SLATE_HERO_PROSE_SURFACES if is_hero else SLATE_PROSE_SURFACES
    payload = build_facts_payload(facts, prose_surfaces=prose_surfaces)
    stmt = select(SummerLeagueDeskSlate).where(
        SummerLeagueDeskSlate.game_id == game_id  # type: ignore[arg-type]
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise ValueError(
            f"No summer_league_desk_slate row for game_id={game_id} -- run "
            "compute_desk_storylines (Job B step 3) before persisting commentary."
        )
    row.facts = payload
    session.add(row)
    await session.flush()
    return payload


async def persist_grade_facts_bulk(
    session: AsyncSession,
    *,
    competition_id: int,
    baseline_version: str,
    facts_by_player: Mapping[int, Sequence[Fact]],
) -> dict[int, list[dict[str, Any]]]:
    """Render + write ``facts`` onto every graded player's T2 row in O(1) queries (#548).

    Bulk sibling of :func:`persist_grade_facts` -- replaces a per-player loop
    (one ``select`` + one ``flush`` PER PLAYER, the tick's step-5 N+1 this
    ticket removes) with exactly ONE ``select`` (every matching T2 row,
    batched by ``player_id.in_(...)``) followed by ONE bulk ``UPDATE``
    (SQLAlchemy's ``executemany``-style bulk update: one compiled statement,
    one round trip, applied per matched row's own ``id`` -- never a
    per-player ``execute``/``flush``). Row-for-row, the rendered payload is
    identical to what N calls to :func:`persist_grade_facts` would have
    produced (both call the same pure :func:`build_facts_payload`).

    Does not create T2 rows -- every ``player_id`` in ``facts_by_player``
    must already have a matching row (mirrors :func:`persist_grade_facts`).
    Does not commit; the caller controls the transaction.

    Args:
        session: Active database session.
        competition_id: The event every row belongs to.
        baseline_version: Which T1 baseline version every row was graded
            against (part of T2's unique key).
        facts_by_player: ``player_id -> every Fact fired for them this tick``.
            Empty returns ``{}`` with no queries at all.

    Returns:
        ``player_id -> persisted payload`` (see :func:`build_facts_payload`).

    Raises:
        ValueError: Some ``player_id`` in ``facts_by_player`` has no matching
            T2 row yet.
    """
    if not facts_by_player:
        return {}

    player_ids = list(facts_by_player.keys())
    stmt = select(SummerLeagueDeskPlayerGrade).where(
        SummerLeagueDeskPlayerGrade.player_id.in_(player_ids),  # type: ignore[attr-defined]
        SummerLeagueDeskPlayerGrade.competition_id == competition_id,  # type: ignore[arg-type]
        SummerLeagueDeskPlayerGrade.baseline_version == baseline_version,  # type: ignore[arg-type]
    )
    rows = (await session.execute(stmt)).scalars().all()
    row_by_player = {row.player_id: row for row in rows}

    missing = [pid for pid in player_ids if pid not in row_by_player]
    if missing:
        raise ValueError(
            f"No summer_league_desk_player_grades row for player_id(s)={missing}, "
            f"competition_id={competition_id}, baseline_version={baseline_version!r} "
            "-- run grade_player_event/grade_players_bulk (Job B step 2) before "
            "persisting commentary."
        )

    payload_by_player: dict[int, list[dict[str, Any]]] = {}
    update_params: list[dict[str, object]] = []
    for player_id, facts in facts_by_player.items():
        payload = build_facts_payload(facts, prose_surfaces=GRADE_PROSE_SURFACES)
        payload_by_player[player_id] = payload
        update_params.append({"_id": row_by_player[player_id].id, "_facts": payload})

    if update_params:
        # `dml_strategy="core_only"`: forces a plain executemany-style Core
        # UPDATE (one compiled statement, one round trip against a list of
        # parameter sets) instead of SQLAlchemy 2.0's ORM-enabled "bulk
        # UPDATE by primary key" fast path, which demands its parameter dict
        # keys match mapped attribute names exactly (not arbitrary bindparam
        # names) and would otherwise misfire here.
        # `synchronize_session=None`: skips syncing this session's identity
        # map from the UPDATE -- which would otherwise leave the
        # `row_by_player` objects fetched above (and any other already-loaded
        # row sharing a PK) stale for the rest of the transaction -- so the
        # `session.expire(...)` calls below are load-bearing, not decorative:
        # they force the NEXT read of each touched row's `facts` (e.g.
        # `_assemble_ledger`'s own T2 read later in this same tick's
        # transaction) to come from the fresh column data any subsequent
        # `select()` returns, rather than the pre-update value cached in
        # memory.
        bulk_stmt = (
            update(SummerLeagueDeskPlayerGrade)
            .where(SummerLeagueDeskPlayerGrade.id == bindparam("_id"))  # type: ignore[arg-type]
            .values(facts=bindparam("_facts"))
            .execution_options(synchronize_session=None, dml_strategy="core_only")
        )
        await session.execute(bulk_stmt, update_params)
        await session.flush()
        for row in row_by_player.values():
            session.expire(row, ["facts"])

    return payload_by_player


async def persist_slate_facts_bulk(
    session: AsyncSession,
    *,
    facts_by_game: Mapping[int, Sequence[Fact]],
    hero_game_ids: Sequence[int] = (),
) -> dict[int, list[dict[str, Any]]]:
    """Render + write ``facts`` onto every touched game's T4 row in O(1) queries (#548).

    Bulk sibling of :func:`persist_slate_facts` -- replaces a per-game loop
    (one ``select`` + one ``flush`` PER GAME) with exactly ONE ``select``
    (every matching T4 row, batched by ``game_id.in_(...)``) followed by ONE
    bulk ``UPDATE`` (same ``executemany``-style pattern as
    :func:`persist_grade_facts_bulk` -- see its docstring). Row-for-row, the
    rendered payload is identical to what N calls to
    :func:`persist_slate_facts` would have produced.

    Does not create T4 rows -- every ``game_id`` in ``facts_by_game`` must
    already have a matching row. Does not commit; the caller controls the
    transaction.

    Args:
        session: Active database session.
        facts_by_game: ``game_id -> every Fact fired for that game's tracked
            subjects this tick``. Empty returns ``{}`` with no queries.
        hero_game_ids: Which of ``facts_by_game``'s keys are today's hero
            game(s) (mirrors :func:`persist_slate_facts`'s ``is_hero`` flag --
            in practice at most one per competition, but a caller batching
            more than one competition's slate in a single call can pass more
            than one).

    Returns:
        ``game_id -> persisted payload`` (see :func:`build_facts_payload`).

    Raises:
        ValueError: Some ``game_id`` in ``facts_by_game`` has no matching T4
            row yet.
    """
    if not facts_by_game:
        return {}

    hero_ids = set(hero_game_ids)
    game_ids = list(facts_by_game.keys())
    stmt = select(SummerLeagueDeskSlate).where(
        SummerLeagueDeskSlate.game_id.in_(game_ids)  # type: ignore[attr-defined]
    )
    rows = (await session.execute(stmt)).scalars().all()
    row_by_game = {row.game_id: row for row in rows}

    missing = [gid for gid in game_ids if gid not in row_by_game]
    if missing:
        raise ValueError(
            f"No summer_league_desk_slate row for game_id(s)={missing} -- run "
            "compute_desk_storylines (Job B step 3) before persisting commentary."
        )

    payload_by_game: dict[int, list[dict[str, Any]]] = {}
    update_params: list[dict[str, object]] = []
    for game_id, facts in facts_by_game.items():
        prose_surfaces = (
            SLATE_HERO_PROSE_SURFACES if game_id in hero_ids else SLATE_PROSE_SURFACES
        )
        payload = build_facts_payload(facts, prose_surfaces=prose_surfaces)
        payload_by_game[game_id] = payload
        update_params.append({"_id": row_by_game[game_id].id, "_facts": payload})

    if update_params:
        # See `persist_grade_facts_bulk` for why `synchronize_session=None`
        # plus the targeted `session.expire(...)` calls below are both
        # required here.
        bulk_stmt = (
            update(SummerLeagueDeskSlate)
            .where(SummerLeagueDeskSlate.id == bindparam("_id"))  # type: ignore[arg-type]
            .values(facts=bindparam("_facts"))
            .execution_options(synchronize_session=None, dml_strategy="core_only")
        )
        await session.execute(bulk_stmt, update_params)
        await session.flush()
        for row in row_by_game.values():
            session.expire(row, ["facts"])

    return payload_by_game


__all__ = [
    "GRADE_PROSE_SURFACES",
    "SLATE_HERO_PROSE_SURFACES",
    "SLATE_PROSE_SURFACES",
    "build_facts_payload",
    "humanize_cohort_key",
    "metric_label",
    "persist_grade_facts",
    "persist_grade_facts_bulk",
    "persist_slate_facts",
    "persist_slate_facts_bulk",
    "render_chip",
    "render_fact",
    "render_prose_for_surface",
]

"""The capability model -- computable metrics are derived, not gated.

This is the one genuinely new component Phase 2 adds (T8, #728); everything else in the phase
deletes duplicated formula copies. Doc #2 §3 / doc #1 item 1.5 describe the gap it closes: there
is no ``metric -> required inputs -> source provides`` mapping today. Availability is expressed
as coarse per-pool booleans (``pbp_available``, ``shotchart_available``, ``adv_eligible``,
``data_quality``) plus scattered inline null-checks (the ``astd_pct`` example named in the
ticket), so a metric can be silently ``None`` at one call site and computed at another.

**The derivation.** A source declares which canonical input tokens it *provides* -- box fields,
PBP-derived counts, team/opponent box, pool context. A metric is computable from that source when
its (transitively resolved) ``requires`` is a subset of ``provides``:

    metric.resolved_requires <= source.provides

A metric whose inputs are unavailable is *structurally absent* -- derived, not conditionally
``None`` by a hand-rolled check that has to be kept in sync with the registry by hand.

**Why "resolved" requires, not the raw field.** Composite-of-composite entries in
:mod:`app.services.stats.registry` list other *metric_keys* in their own ``requires`` rather than
raw :class:`~app.services.stats.inputs.StatInputs` fields -- ``ws40.requires == ("ws", "mp")``,
and the same shape for ``net_rating``, ``ws82``, ``vorp``, ``vorp82``. Testing
``metric.requires <= provides`` as a flat set operation would report every one of these as
uncomputable from *any* source, because no source declares ``"ws"`` itself as a provided input.
:func:`resolve_requires` substitutes a metric-key entry with that metric's own (recursively
resolved) ``requires`` before the subset test, with a cycle guard so a mutually-referential
registry entry fails loudly (a :class:`ValueError`) instead of recursing forever.

**The box-vs-PBP split has a clean signal, not a hand-maintained list.** PBP-derived metrics use
PBP-specific token names deliberately distinct from box field names -- ``astd_pct.requires``
contains ``ast_fgm``/``unast_fgm``, never the box's plain ``ast``. A caller building a
"box-only" ``provides`` set simply omits the PBP tokens; it does not need to enumerate which
metric keys are "the PBP ones" by name (see
``app.services.sources.summer_league.capabilities`` for the Summer League adapter that builds
``provides`` sets from the SL-specific availability flags).

**What this module deliberately does not do.** It does not decide *which* inputs a source
provides -- that mapping is source-specific and belongs in that source's own package (Summer
League's lives under ``app.services.sources.summer_league``, per import contract 3: nothing here
may import ``app.services.sources.summer_league*``, the ``app.services.summer_league_*``
read-side siblings, or ``app.schemas.summer_league*``). It also does not wire
up ``environment_turnover_rate`` (the frozen exemption declared in
:mod:`app.services.stats.registry`) as an alternate source for ``tov_pct`` or treat it specially
in any way -- it is just another registered metric_key whose ``requires`` happens to resolve to
raw box fields like every other entry; nothing in this module singles it out.
"""

from __future__ import annotations

from app.services.stats.registry import METRICS_BY_KEY, get_metric


def resolve_requires(metric_key: str) -> frozenset[str]:
    """Transitively resolve ``metric_key``'s ``requires`` into raw input tokens.

    When an entry in a metric's ``requires`` is itself a declared ``metric_key`` (a
    composite-of-composite, e.g. ``ws40.requires == ("ws", "mp")``), it is replaced by that
    metric's own resolved requires rather than treated as a literal (and unsatisfiable) input
    token. Tokens that are not themselves metric keys (raw :class:`StatInputs` fields, or the
    structural markers ``"team_box"``, ``"opponent_box"``, ``"pool_context"``, or a PBP token
    like ``"ast_fgm"``) pass through unchanged.

    Args:
        metric_key: A key declared in :data:`app.services.stats.registry.METRICS_BY_KEY`.

    Returns:
        The flat set of raw input tokens the metric ultimately needs.

    Raises:
        KeyError: ``metric_key`` is not declared in the registry.
        ValueError: the ``requires`` chain cycles back on itself.
    """
    return _resolve(metric_key, frozenset())


def _resolve(metric_key: str, stack: frozenset[str]) -> frozenset[str]:
    if metric_key in stack:
        chain = " -> ".join((*stack, metric_key))
        raise ValueError(f"cycle detected resolving metric requires: {chain}")
    metric = get_metric(metric_key)  # raises KeyError for an unknown key, loudly
    next_stack = stack | {metric_key}
    resolved: set[str] = set()
    for token in metric.requires:
        if token in METRICS_BY_KEY:
            resolved |= _resolve(token, next_stack)
        else:
            resolved.add(token)
    return frozenset(resolved)


def is_computable(metric_key: str, provides: frozenset[str]) -> bool:
    """Whether ``metric_key`` is computable from a source declaring ``provides``.

    Args:
        metric_key: A key declared in the registry.
        provides: The canonical input tokens the source/event makes available.

    Returns:
        ``True`` when the metric's transitively resolved ``requires`` is a subset of
        ``provides`` -- ``metric.resolved_requires <= source.provides``. A metric declared with
        an empty ``requires`` would trivially be computable from any source, but the registry's
        own acceptance criteria (T7, #724) guarantee no entry has an empty ``requires``.
    """
    return resolve_requires(metric_key) <= provides


def computable_metrics(provides: frozenset[str]) -> frozenset[str]:
    """Metric keys whose (transitively resolved) ``requires`` are satisfied by ``provides``."""
    return frozenset(
        metric_key
        for metric_key in METRICS_BY_KEY
        if is_computable(metric_key, provides)
    )

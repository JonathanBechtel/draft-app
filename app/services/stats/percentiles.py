"""One percentile primitive, forward and reverse.

Two functions, matching numpy's default ``'linear'`` interpolation method:

* :func:`percentile` -- forward lookup, the value at quantile ``q`` (0-1) within a
  distribution.
* :func:`percentile_of` -- reverse lookup, the percentile rank (0-100) of a value within a
  ``{percentile_str: value}`` breakpoints grid (as built by repeated calls to
  :func:`percentile`).

Both are deliberately the **unrounded** primitive. Rounding and empty-input handling are a
caller decision -- three call sites disagreed on both before this module existed (0-100 vs.
0-1 inputs, ``round(..., 2)`` vs. none, raise vs. ``{}`` on empty), so pushing either choice
into the shared function would move a caller's stored values. Callers round and guard empty
input themselves; see ``app.services.summer_league_environment_service._percentile``,
``app.services.sources.summer_league.cohort_baselines.compute_breakpoints``, and
``app.services.sources.summer_league.desk_grades.percentile_of_value``.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

__all__ = ["percentile", "percentile_of"]


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile at ``q`` (0-1) over ``values``.

    Matches numpy's default ``'linear'`` method. Unrounded -- callers round if needed.

    Args:
        values: The distribution to sample, any order.
        q: The quantile to sample, in ``[0, 1]``.

    Returns:
        The interpolated value at quantile ``q``.

    Raises:
        ValueError: ``values`` is empty.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty sequence")
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * q
    low = math.floor(idx)
    high = math.ceil(idx)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - idx) + ordered[high] * (idx - low)


def percentile_of(breakpoints: Mapping[str, float], value: float) -> float:
    """Reverse lookup: the percentile rank of ``value`` within ``breakpoints``.

    Inverts a ``{"0": ..., "5": ..., ..., "100": ...}`` percentile -> value grid (as fit by
    repeated calls to :func:`percentile`) via linear interpolation, mirroring numpy's
    ``'linear'`` method so a value exactly at a fitted breakpoint returns exactly that
    percentile.

    Args:
        breakpoints: Percentile (string keys, 0-100) -> value grid. Must be non-empty --
            callers guard the empty case themselves, since they disagree on how to handle it.
        value: The value to rank.

    Returns:
        The interpolated percentile, 0-100 (clamped at the grid's ends for values outside the
        observed range). Unrounded -- callers round if needed.

    Raises:
        ValueError: ``breakpoints`` is empty.
    """
    if not breakpoints:
        raise ValueError("Cannot rank a value against an empty breakpoints map.")

    points = sorted(((int(p), v) for p, v in breakpoints.items()), key=lambda t: t[0])
    lowest_p, lowest_v = points[0]
    highest_p, highest_v = points[-1]

    if value <= lowest_v:
        return float(lowest_p)
    if value >= highest_v:
        return float(highest_p)

    for (p_lo, v_lo), (p_hi, v_hi) in zip(points, points[1:]):
        if v_lo <= value <= v_hi:
            # v_hi == v_lo (a flat plateau) never reaches here as the first bracketing
            # pair: the pre-loop clamps above already catch a plateau starting at index 0,
            # and a later plateau is always caught first by the non-degenerate pair
            # transitioning into it (whose v_hi already equals the plateau's value).
            frac = (value - v_lo) / (v_hi - v_lo)
            return p_lo + frac * (p_hi - p_lo)

    # Unreachable: the grid is sorted and covers [lowest_v, highest_v], and value is
    # already known to fall inside that range at this point.
    return float(highest_p)

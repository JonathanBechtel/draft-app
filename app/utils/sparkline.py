"""Tiny SVG sparkline path builder for consensus rank trajectories.

The consensus board renders one sparkline per row showing the player's
recent rank history. Lower ranks (better) plot higher; the path is
normalized to each series' own min/max so motion is visible even for
narrow drifts. Returned values are SVG ``d`` attribute strings the
template drops into a ``<path>``.
"""

from __future__ import annotations

from typing import Optional


def build_sparkline_path(
    ranks: list[int],
    *,
    width: int = 80,
    height: int = 18,
    padding: int = 2,
) -> Optional[str]:
    """Return an SVG ``d`` attribute string for a rank-trajectory sparkline.

    Args:
        ranks: Player's consensus rank at each snapshot, oldest-first.
        width: SVG viewport width in user units.
        height: SVG viewport height in user units.
        padding: Inner margin so the line never touches the box edges.

    Returns:
        An ``M x0,y0 L x1,y1 ...`` path string, or ``None`` when there
        is fewer than two points (no line to draw).
    """
    if len(ranks) < 2:
        return None
    lo = min(ranks)
    hi = max(ranks)
    rng = max(1, hi - lo)  # avoid division by zero on a perfectly flat series
    inner_w = width - 2 * padding
    inner_h = height - 2 * padding
    n = len(ranks) - 1
    points: list[str] = []
    for i, r in enumerate(ranks):
        x = padding + (i / n) * inner_w
        # Lower rank (better) sits higher on screen → y closer to 0.
        y = padding + ((r - lo) / rng) * inner_h
        points.append(f"{x:.1f},{y:.1f}")
    return "M " + " L ".join(points)


def sparkline_direction(ranks: list[int]) -> str:
    """Return ``'up'`` / ``'down'`` / ``'flat'`` for trend coloring.

    ``up`` = rank improved (lower number at the end of the series),
    ``down`` = worsened, ``flat`` = unchanged or insufficient data.
    """
    if len(ranks) < 2:
        return "flat"
    if ranks[-1] < ranks[0]:
        return "up"
    if ranks[-1] > ranks[0]:
        return "down"
    return "flat"

"""One per-mode counting-stat scaling definition, in Python and SQL-fragment form.

Every Summer League surface that shows counting stats (points, rebounds, Game Score, ...)
offers four views: per-game, per-36, per-100, and totals. Before this module the arithmetic
for scaling a summed box total into one of those views was written three times in
``app.services.summer_league_explorer_service`` -- the Python display path
(``_compute_player_values``), the SQL sort path (``_scaled_sort_expr``), and a third time
inside the multi-competition ``rollup_recombinable`` rollup for ``pts_per100``. Two of the
three carried a docstring admitting the duplication ("Mirrors the arithmetic in
``_compute_player_values``"). :func:`scale_python` and :func:`scale_sql` are now the one
place that arithmetic is stated; every call site delegates here.

**The four factors -- verified at HEAD (Phase 2, T4 / issue #725). Do not "correct" them:**

* ``per_game`` -- ``value * 1.0 / gp``. The ``* 1.0`` forces float division: counts/totals
  are integers in Postgres, and integer division would truncate the rate into
  non-monotonic ties.
* ``per_36`` -- ``value * 2160.0 / seconds``. 36 minutes * 60 seconds.
* ``per_100`` -- ``value * 288000.0 / pace_seconds``. 100 possessions, where
  ``possessions = pace_seconds / (60 * 48)``.
* ``totals`` -- ``value``, unscaled.

**Summer League pace is possessions per 48 minutes, not 40** (see
``app.services.summer_league.constants.MINUTES_PER_GAME``) -- the 48 is baked into the
``288000.0`` numerator (``100 * 60 * 48``), even though Summer League games run 40 minutes.
"Fixing" it to 40, or dropping the ``per_game`` ``* 1.0``, silently breaks the Explorer's
sort order.

A zero/absent denominator scales to ``None`` in Python and to SQL ``NULL`` (via ``NULLIF``)
in every mode but ``totals``, which never divides. This module only owns the arithmetic --
callers that need a specific null-sort position (e.g. "rows without pace sort last in
per_100") still apply their own ``NULLS LAST``/``COALESCE`` on top; see
``_player_sort_expr``/``_player_sort_expr_career`` in
``app.services.summer_league_explorer_service`` for that layering.

This module lives in the shared engine package (``app/services/stats/``) and is therefore
bound by import contract 3 in ``pyproject.toml``: it may not import anything under
``app.services.summer_league*`` or ``app.schemas.summer_league*``.
"""

from __future__ import annotations

from typing import Optional

# 36 minutes * 60 seconds.
_PER_36_NUMERATOR = 2160.0
# 100 possessions * 60 seconds * 48 minutes -- Summer League's per-48 pace base (see the
# module docstring). NOT per-40, even though Summer League games run 40 minutes.
_PER_100_NUMERATOR = 288000.0


def scale_python(
    value: float,
    mode: str,
    *,
    gp: int,
    seconds: float,
    pace_seconds: float,
) -> Optional[float]:
    """Scale a summed box total into the selected per-mode view.

    Mirrors :func:`scale_sql` exactly so a SQL ``ORDER BY`` built from that function
    ranks a row on precisely the value this function renders for the same cell. Game
    Score is linear in the box stats, so scaling a summed Game Score total by this same
    per-mode factor is exact -- it does not need a separate formula.

    Args:
        value: The summed box total to scale (e.g. ``SUM(pts)``, or a summed Game Score).
        mode: One of ``"per_game"``, ``"per_36"``, ``"per_100"``, ``"totals"``.
        gp: Games played -- the ``per_game`` denominator.
        seconds: Seconds played -- the ``per_36`` denominator.
        pace_seconds: Pace-weighted seconds -- the ``per_100`` denominator (possessions =
            ``pace_seconds / (60 * 48)``).

    Returns:
        The scaled value, unrounded -- callers apply their own display rounding. ``None``
        when the mode's denominator is zero/falsy, matching SQL's ``NULLIF``-guarded
        division in :func:`scale_sql`.
    """
    if mode == "per_game":
        return value * 1.0 / gp if gp else None
    if mode == "per_36":
        return value * _PER_36_NUMERATOR / seconds if seconds else None
    if mode == "per_100":
        return value * _PER_100_NUMERATOR / pace_seconds if pace_seconds else None
    return value  # totals


def scale_sql(num: str, gp: str, sec: str, pace_sec: str, mode: str) -> str:
    """Build the SQL fragment that scales ``num`` into the selected per-mode view.

    Mirrors :func:`scale_python` exactly so an ``ORDER BY`` built from this expression
    ranks a row on precisely the value the Python display path renders for that cell --
    guarding against the SQL-sort ``COALESCE`` gotcha, where a sort expression's null
    handling drifts from the display path's and a row sorts differently from how it
    renders.

    Args:
        num: SQL fragment for the numerator (e.g. ``"SUM(pts)"`` or a raw column label).
        gp: SQL fragment for games played.
        sec: SQL fragment for seconds played.
        pace_sec: SQL fragment for pace-weighted seconds.
        mode: One of ``"per_game"``, ``"per_36"``, ``"per_100"``, ``"totals"``.

    Returns:
        A SQL text fragment. Every mode but ``totals`` guards its denominator with
        ``NULLIF`` so a zero/absent denominator yields SQL ``NULL`` rather than an error
        or a wrong tie.
    """
    if mode == "per_game":
        # * 1.0 forces float division (counts/totals are integers in Postgres, and
        # integer division would truncate the rate into non-monotonic ties).
        return f"{num} * 1.0 / NULLIF({gp}, 0)"
    if mode == "per_36":  # 36 min / (sec/60) = num * 36 * 60 / sec
        return f"{num} * {_PER_36_NUMERATOR} / NULLIF({sec}, 0)"
    if mode == "per_100":  # 100 poss; poss = pace_sec / (60 * 48)
        return f"{num} * {_PER_100_NUMERATOR} / NULLIF({pace_sec}, 0)"
    return num  # totals

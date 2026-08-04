"""Neutral box-total and pool-context inputs for the shared stat engine.

These are the shapes every formula in :mod:`app.services.stats.formulas` is built
on:

* :class:`StatInputs` (formerly ``Box`` in ``app.services.sources.summer_league.metrics``)
  -- a summed box line (counting stats + minutes), with :meth:`StatInputs.add_row`
  accumulating any duck-typed row that exposes the same field names and
  :meth:`StatInputs.poss` estimating BBRef possessions against an opponent box.
* :class:`PoolContext` (formerly ``LeagueContext``) -- the recalibration constants
  (pace, VOP, DRB%, the PER standardization scalar) derived from one pool's summed
  totals via :meth:`PoolContext.finalize`.
* :class:`PlayerSeason` -- the per-(player, year, venue) DTO
  :func:`app.services.stats.formulas.compute_metrics` populates. It carries only
  :class:`StatInputs` and plain scalars; Summer League is its first caller, not
  its shape.

None of these import anything Summer-League-specific; ``add_row``'s row argument
is duck-typed by design (see the package docstring in
``app/services/stats/__init__.py``) so a caller can hand it any object exposing
the box counting-stat fields by name, regardless of source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _d(num: float, den: float) -> float:
    """Safe divide: 0.0 when the denominator is zero (small-sample guard)."""
    return num / den if den else 0.0


# The counting-stat fields a box line accumulates via `StatInputs.add_row`, and
# the fields the Summer League loader/persistence layer sums or stores by the
# same names (`app/services/summer_league/metrics.py` re-exports this tuple as
# `_BOX_INT_FIELDS`).
BOX_INT_FIELDS = (
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "pts",
)


@dataclass
class StatInputs:
    """A summed box line (minutes in *minutes*, not seconds)."""

    mp: float = 0.0
    fgm: float = 0.0
    fga: float = 0.0
    fg3m: float = 0.0
    fg3a: float = 0.0
    ftm: float = 0.0
    fta: float = 0.0
    oreb: float = 0.0
    dreb: float = 0.0
    reb: float = 0.0
    ast: float = 0.0
    stl: float = 0.0
    blk: float = 0.0
    tov: float = 0.0
    pf: float = 0.0
    pts: float = 0.0
    gp: int = 0

    def add_row(self, r: Any) -> None:
        """Accumulate a team or player box row's counting stats.

        Duck-typed: ``r`` need only expose the :data:`BOX_INT_FIELDS` attributes
        by name (a SQLAlchemy result row, an ORM instance, a ``SimpleNamespace``,
        ...). This method must never import a source-specific type -- see the
        import contract in ``app/services/stats/__init__.py``.
        """
        for f in BOX_INT_FIELDS:
            setattr(self, f, getattr(self, f) + (getattr(r, f) or 0))

    def poss(self, opp: "StatInputs") -> float:
        """Bbref estimated team possessions for this box vs an opponent box."""
        return 0.5 * (
            (
                self.fga
                + 0.4 * self.fta
                - 1.07 * _d(self.oreb, self.oreb + opp.dreb) * (self.fga - self.fgm)
                + self.tov
            )
            + (
                opp.fga
                + 0.4 * opp.fta
                - 1.07 * _d(opp.oreb, opp.oreb + self.dreb) * (opp.fga - opp.fgm)
                + opp.tov
            )
        )


@dataclass
class PoolContext:
    """Recalibration constants for one (year, venue) competition pool."""

    competition_id: int
    year: int
    venue: str
    lg: StatInputs
    poss: float
    team_games: int  # team-game rows (2 per game)
    total_games: int = 0  # distinct games (denominator for completeness)
    complete_games: int = 0
    pace: float = 0.0
    pts_per_poss: float = 0.0
    ppg: float = 0.0
    factor: float = 0.0
    vop: float = 0.0
    drb_pct: float = 0.0
    aper_scalar: float = 0.0
    adv_eligible: bool = False

    def finalize(self) -> None:
        """Derive the league constants from the summed league box."""
        lg = self.lg
        self.pace = 48.0 * _d(self.poss, lg.mp / 5.0)
        self.pts_per_poss = _d(lg.pts, self.poss)
        self.ppg = _d(lg.pts, self.team_games)
        self.factor = (2.0 / 3.0) - _d(
            0.5 * _d(lg.ast, lg.fgm), 2.0 * _d(lg.fgm, lg.ftm)
        )
        self.vop = _d(lg.pts, lg.fga - lg.oreb + lg.tov + 0.44 * lg.fta)
        self.drb_pct = _d(lg.reb - lg.oreb, lg.reb)


@dataclass
class PlayerSeason:
    """One (player, year, venue) row with box totals and team/opponent context.

    The DTO :func:`app.services.stats.formulas.compute_metrics` populates -- it
    carries no ORM types, only :class:`StatInputs` and plain scalars.
    """

    player_id: int
    competition_id: int
    primary_team_entry_id: Optional[int]
    year: int
    venue: str
    box: StatInputs
    team: StatInputs
    opp: StatInputs
    pm: float = 0.0
    # Transient computation scratch (not persisted): possession/usage bases and
    # the pre-standardization PER / pre-centering BPM components.
    player_poss: float = 0.0
    pct_min: float = 0.0
    aper: Optional[float] = None
    raw_off: Optional[float] = None
    raw_def: Optional[float] = None
    raw_bpm: Optional[float] = None
    # Minute-weighted NBA Advanced box rates, retained as the source of truth
    # when present. They remain available before team-box completeness reaches
    # the stricter pool-calibration threshold.
    source_rates: dict[str, Optional[float]] = field(default_factory=dict)
    metrics: dict[str, Optional[float]] = field(default_factory=dict)

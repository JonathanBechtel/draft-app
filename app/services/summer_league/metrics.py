"""Compute Summer League advanced metrics, recalibrated to the league itself.

The pipeline turns raw box logs into a full advanced-stat basket per
``(player, year, venue)`` season:

* A :class:`LeagueContext` per competition holds the recalibration constants
  (pace, VOP, DRB%, the PER standardization scalar) built from *summed totals* of
  every team-game in that pool.
* Box / shooting / rate / possession / ratings / PER / Win Shares are computed
  per player from their box totals plus their team's and opponent's totals.
* **Coefficients are derived from actual Summer League data wherever possible:**
  Win Shares' points-to-wins comes from the SL Pythagorean exponent, and BPM is a
  weighted regression of per-100 box stats onto real SL plus-minus (re-centered so
  each pool averages 0.0), rather than borrowing NBA-fit constants.

Pools with thin or incomplete box data are flagged ``adv_eligible=False``; for
those, box/shooting stats plus raw possession and player/team-box rates
(``pace``, ``pts_per100``, usage/rebound %, etc.) are populated. Only metrics that
need a trustworthy league-wide calibration (PER, ORtg, WS, BPM, …) are left
``None``.

This module is pure computation plus a persistence orchestrator; it is invoked
offline by ``scripts/rebuild_sl_metrics.py``, not on the request path.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeagueShotEvent,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueMetricModel,
    SummerLeaguePlayerSeason,
)

# Qualification / gating thresholds.
QUALIFY_MIN_MINUTES = 40.0  # minutes for a season to feed fits / leaders
MIN_COMPLETE_TEAM_MP = 150.0  # sane regulation team minutes (~200 for 40-min)
# Pool-level pace floor below which the possession estimate is not real. Genuine
# Summer League pools run ~85-115; pools reconstructed from season logs without
# team box data (mainly 2012-2016) compute ~0-4. Anything under this floor has no
# trustworthy possessions, so player pace/pts_per100 are left NULL for that pool.
MIN_POOL_PACE = 40.0
ADV_MIN_PLAYERS = 10  # qualified players for a trustworthy pool
ADV_MIN_COMPLETE_FRAC = 0.80  # fraction of complete-box games for a pool
VORP_REPLACEMENT = -2.0  # pts/100 below average (scale convention)

# Shot-diet zone buckets: NBA SHOT_ZONE_BASIC → rate column.
# Backcourt shots are excluded from the denominator (heaves carry no
# scouting signal; same exclusion as summer_league_shotchart_service).
_EXCLUDED_ZONES: frozenset[str] = frozenset({"Backcourt"})

# Map each non-excluded zone to the metric key it contributes to.
_ZONE_TO_BUCKET: dict[str, str] = {
    "Restricted Area": "rim",
    "In The Paint (Non-RA)": "mid",
    "Mid-Range": "mid",
    "Left Corner 3": "three",
    "Right Corner 3": "three",
    "Above the Break 3": "three",
}
# corner3 tracks corner threes as a sub-bucket of three_rate.
_CORNER3_ZONES: frozenset[str] = frozenset({"Left Corner 3", "Right Corner 3"})

# Season aggregates intentionally lag live game reporting.  Current-game facts
# continue to feed Desk Tier 1 directly, while Tier 2 (this materialized
# season table) reflects only completed or historical games.  UNKNOWN is
# deliberately included: historical backfills predate the scoreboard status
# feed and use that schema default.
_SEASON_EXCLUDED_GAME_STATUSES: tuple[SummerLeagueGameStatus, ...] = (
    SummerLeagueGameStatus.SCHEDULED,
    SummerLeagueGameStatus.IN_PROGRESS,
    SummerLeagueGameStatus.POSTPONED,
    SummerLeagueGameStatus.CANCELED,
)


def _season_game_status_clause() -> Any:
    """Return the SQL predicate selecting games eligible for season aggregates."""
    game_status: Any = SummerLeagueGame.status
    return game_status.notin_(_SEASON_EXCLUDED_GAME_STATUSES)


def compute_shot_diet(
    zone_fga: dict[str, int],
) -> dict[str, Optional[float]]:
    """Compute rim/mid/three/corner3 rates from per-zone FGA counts.

    Args:
        zone_fga: Mapping of ``SHOT_ZONE_BASIC`` label → FGA count.
            Backcourt should already be excluded by the query.

    Returns:
        Dict with keys ``rim_rate``, ``mid_rate``, ``three_rate``,
        ``corner3_rate``.  All ``None`` when total FGA is zero (no shot
        data for this player-competition).  Stored as fractions (0.0–1.0),
        rounded to 4 decimal places, matching the ``fg3ar``/``ftr``
        convention on ``SummerLeaguePlayerSeason``.
    """
    total = sum(zone_fga.values())
    if not total:
        return {
            "rim_rate": None,
            "mid_rate": None,
            "three_rate": None,
            "corner3_rate": None,
        }
    buckets: dict[str, int] = {"rim": 0, "mid": 0, "three": 0}
    corner3 = 0
    for zone, fga in zone_fga.items():
        bucket = _ZONE_TO_BUCKET.get(zone)
        if bucket:
            buckets[bucket] += fga
        if zone in _CORNER3_ZONES:
            corner3 += fga
    return {
        "rim_rate": round(buckets["rim"] / total, 4),
        "mid_rate": round(buckets["mid"] / total, 4),
        "three_rate": round(buckets["three"] / total, 4),
        "corner3_rate": round(corner3 / total, 4),
    }


# Non-collinear per-100 box predictors for the BPM regression (pts omitted — it
# is a linear combination of made shots, already present as features).
BPM_FEATURES = (
    "fg2m",
    "fg3m",
    "ftm",
    "fg_miss",
    "ft_miss",
    "orb",
    "drb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
)
OFF_FEATURES = frozenset(
    {"fg2m", "fg3m", "ftm", "fg_miss", "ft_miss", "orb", "ast", "tov"}
)
DEF_FEATURES = frozenset({"drb", "stl", "blk", "pf"})

_BOX_INT_FIELDS = (
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

# NBA's player Advanced box endpoint supplies these player/team-box rates even
# while the corresponding team Advanced rows are incomplete. Keep the source
# names separate from the materialized season names: NBA calls total rebound
# percentage ``REB_PCT`` while the app's metric schema uses ``trb_pct``.
_SOURCE_RATE_COLUMNS: dict[str, str] = {
    "usg_pct": "usg_pct",
    "ast_pct": "ast_pct",
    "trb_pct": "reb_pct",
}


def _d(num: float, den: float) -> float:
    """Safe divide: 0.0 when the denominator is zero (small-sample guard)."""
    return num / den if den else 0.0


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
@dataclass
class Box:
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
        """Accumulate a team or player box row's counting stats."""
        for f in _BOX_INT_FIELDS:
            setattr(self, f, getattr(self, f) + (getattr(r, f) or 0))

    def poss(self, opp: "Box") -> float:
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
class LeagueContext:
    """Recalibration constants for one (year, venue) competition pool."""

    competition_id: int
    year: int
    venue: str
    lg: Box
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
    """One (player, year, venue) row with box totals and team/opponent context."""

    player_id: int
    competition_id: int
    primary_team_entry_id: Optional[int]
    year: int
    venue: str
    box: Box
    team: Box
    opp: Box
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


# --------------------------------------------------------------------------- #
# Per-player metric computation
# --------------------------------------------------------------------------- #
def game_score(b: Box) -> float:
    """Hollinger Game Score (no league constants)."""
    return (
        b.pts
        + 0.4 * b.fgm
        - 0.7 * b.fga
        - 0.4 * (b.fta - b.ftm)
        + 0.7 * b.oreb
        + 0.3 * b.dreb
        + b.stl
        + 0.7 * b.ast
        + 0.7 * b.blk
        - 0.4 * b.pf
        - b.tov
    )


def game_score_line(
    *,
    pts: Any,
    fgm: Any,
    fga: Any,
    ftm: Any,
    fta: Any,
    oreb: Any,
    dreb: Any,
    ast: Any,
    stl: Any,
    blk: Any,
    tov: Any,
    pf: Any,
) -> float:
    """Hollinger Game Score from raw box components, ``None``-coalescing to 0.

    A single source of truth for the read-side surfaces (player-detail season +
    game logs, the explorer) that need Game Score from a box line — be it one
    game, a summed competition, or a summed career. Because Game Score is linear
    in the box stats, ``game_score_line(summed) / gp`` equals the mean per-game
    Game Score, matching the materialized ``SummerLeaguePlayerSeason.gmsc``.
    """
    return game_score(
        Box(
            pts=float(pts or 0),
            fgm=float(fgm or 0),
            fga=float(fga or 0),
            ftm=float(ftm or 0),
            fta=float(fta or 0),
            oreb=float(oreb or 0),
            dreb=float(dreb or 0),
            ast=float(ast or 0),
            stl=float(stl or 0),
            blk=float(blk or 0),
            tov=float(tov or 0),
            pf=float(pf or 0),
        )
    )


# The twelve box components Game Score weights, in :func:`game_score_line` order.
_GAME_SCORE_BOX_FIELDS = (
    "pts",
    "fgm",
    "fga",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
)


def game_score_from_row(row: Any) -> float:
    """Game Score from any row exposing the box components (missing/None → 0).

    Accepts either a mapping of summed totals or an object with the box-stat
    attributes (a SQLAlchemy result row, an ORM instance, a ``SimpleNamespace``),
    so every read surface funnels through one field-mapping path instead of
    repeating the twelve-keyword :func:`game_score_line` call.
    """
    if isinstance(row, Mapping):
        return game_score_line(**{f: row.get(f, 0) for f in _GAME_SCORE_BOX_FIELDS})
    return game_score_line(**{f: getattr(row, f, 0) for f in _GAME_SCORE_BOX_FIELDS})


# --------------------------------------------------------------------------- #
# Line-grain advanced rates
# --------------------------------------------------------------------------- #
# Single-line twins of the season formulas in :func:`compute_metrics`, for read
# surfaces that need a rate from one box line (a single game). They match the
# season math exactly but return ``None`` on an empty denominator — a single
# game with no qualifying attempts renders an em-dash, whereas the materialized
# season columns store 0.0 via ``_d``.


def ftr_line(*, fga: Any, fta: Any) -> Optional[float]:
    """Free-throw rate (FTA / FGA) for one box line, ``None``-coalescing to 0.

    Returns:
        The 0–1 fraction rounded to 3 decimals (the scale ``compute_metrics``
        stores), or ``None`` when the line has no field-goal attempts.
    """
    fga_f = float(fga or 0)
    if fga_f <= 0:
        return None
    return round(float(fta or 0) / fga_f, 3)


def tov_pct_line(*, fga: Any, fta: Any, tov: Any) -> Optional[float]:
    """Turnover % — 100 · TOV / (FGA + 0.44 · FTA + TOV) — for one box line.

    Returns:
        The 0–100 percentage rounded to 1 decimal, or ``None`` when the line
        has no true-shooting attempts or turnovers to divide by.
    """
    den = float(fga or 0) + 0.44 * float(fta or 0) + float(tov or 0)
    if den <= 0:
        return None
    return round(100.0 * float(tov or 0) / den, 1)


def game_advanced_line(
    b: Box, tm: Optional[Box] = None, opp: Optional[Box] = None
) -> dict[str, Optional[float]]:
    """Full BBRef-style single-game advanced line from player/team/opponent boxes.

    Mirrors the season-grain formulas in :func:`compute_metrics` for one game:
    attempt rates from the player's own line; rebound/steal/block/assist rates
    against the team + opponent context; ORtg/DRtg via the Dean Oliver
    individual-rating formulas (:func:`compute_ortg` / :func:`compute_drtg`).
    Each metric is ``None`` when its denominator is empty (e.g. a 0-FGA line has
    no ORtg) rather than 0, so single-game cells render as em-dashes.

    Args:
        b:   The player's single-game box (minutes in minutes).
        tm:  The player's team full-game box, when its team log exists.
             Without it only the player-only rates compute.
        opp: The opposing team's full-game box, when its log exists. The
             opponent-relative rates (rebound/steal/block %s, ORtg/DRtg) need
             both team boxes.

    Returns:
        Dict keyed ``fg3ar``/``ftr`` (0-1 fractions, 3 dp), the participation
        percentages (0-100, 1 dp), and ``ortg``/``drtg`` (points per 100
        possessions, 1 dp).
    """
    out: dict[str, Optional[float]] = {
        "fg3ar": round(b.fg3a / b.fga, 3) if b.fga > 0 else None,
        "ftr": ftr_line(fga=b.fga, fta=b.fta),
        "tov_pct": tov_pct_line(fga=b.fga, fta=b.fta, tov=b.tov),
        "ast_pct": (
            ast_pct_line(ast=b.ast, fgm=b.fgm, mp=b.mp, tm_mp=tm.mp, tm_fgm=tm.fgm)
            if tm is not None
            else None
        ),
        "orb_pct": None,
        "drb_pct": None,
        "trb_pct": None,
        "stl_pct": None,
        "blk_pct": None,
        "ortg": None,
        "drtg": None,
    }
    if tm is None or opp is None:
        return out

    def _rate(num: float, den: float) -> Optional[float]:
        return round(100.0 * num / den, 1) if den > 0 else None

    tm_mp5 = tm.mp / 5.0
    if tm_mp5 > 0 and b.mp > 0:
        out["orb_pct"] = _rate(b.oreb * tm_mp5, b.mp * (tm.oreb + opp.dreb))
        out["drb_pct"] = _rate(b.dreb * tm_mp5, b.mp * (tm.dreb + opp.oreb))
        out["trb_pct"] = _rate(b.reb * tm_mp5, b.mp * (tm.reb + opp.reb))
        out["stl_pct"] = _rate(b.stl * tm_mp5, b.mp * opp.poss(tm))
        out["blk_pct"] = _rate(b.blk * tm_mp5, b.mp * (opp.fga - opp.fg3a))

    ortg, tot_poss, _pprod = compute_ortg(b, tm, opp)
    out["ortg"] = round(ortg, 1) if tot_poss > 0 else None
    tm_poss = tm.poss(opp)
    out["drtg"] = (
        round(compute_drtg(b, tm, opp, tm_poss), 1)
        if (tm_poss > 0 and b.mp > 0)
        else None
    )
    return out


def ast_pct_line(
    *,
    ast: Any,
    fgm: Any,
    mp: Any,
    tm_mp: Any,
    tm_fgm: Any,
) -> Optional[float]:
    """Assist % — share of teammate field goals a player assisted while on floor.

    Bbref's estimate: ``100 · AST / ((MP / (Tm MP / 5)) · Tm FGM − FGM)``, the
    same formula :func:`compute_metrics` applies at season grain. ``tm_mp`` is
    the team's total player-minutes (≈ 240 for one regulation game).

    Returns:
        The 0–100 percentage rounded to 1 decimal, or ``None`` when team
        context is missing or the teammate-FG denominator is not positive
        (e.g. the player scored every on-floor team basket).
    """
    tm_mp5 = float(tm_mp or 0) / 5.0
    if tm_mp5 <= 0:
        return None
    den = (float(mp or 0) / tm_mp5) * float(tm_fgm or 0) - float(fgm or 0)
    if den <= 0:
        return None
    return round(100.0 * float(ast or 0) / den, 1)


def compute_uper(b: Box, tm: Box, ctx: LeagueContext) -> float:
    """Bbref unadjusted PER from a player's box + team totals + league context."""
    if b.mp <= 0:
        return 0.0
    factor, vop, drbp = ctx.factor, ctx.vop, ctx.drb_pct
    tm_ast_fg = _d(tm.ast, tm.fgm)
    lg = ctx.lg
    return (1.0 / b.mp) * (
        b.fg3m
        + (2.0 / 3.0) * b.ast
        + (2.0 - factor * tm_ast_fg) * b.fgm
        + (b.ftm * 0.5 * (1.0 + (1.0 - tm_ast_fg) + (2.0 / 3.0) * tm_ast_fg))
        - vop * b.tov
        - vop * drbp * (b.fga - b.fgm)
        - vop * 0.44 * (0.44 + 0.56 * drbp) * (b.fta - b.ftm)
        + vop * (1.0 - drbp) * (b.reb - b.oreb)
        + vop * drbp * b.oreb
        + vop * b.stl
        + vop * drbp * b.blk
        - b.pf * (_d(lg.ftm, lg.pf) - 0.44 * _d(lg.fta, lg.pf) * vop)
    )


def compute_ortg(b: Box, tm: Box, opp: Box) -> tuple[float, float, float]:
    """Dean Oliver individual offensive rating.

    Returns ``(ortg, total_possessions, points_produced)``.
    """
    if b.mp <= 0 or b.fga <= 0:
        return 0.0, 0.0, 0.0
    tm_mp5 = _d(tm.mp, 5.0)
    qast = (_d(b.mp, tm_mp5) * (1.14 * _d(tm.ast - b.ast, tm.fgm))) + (
        _d(
            (_d(tm.ast, tm.mp) * b.mp * 5.0 - b.ast),
            (_d(tm.fgm, tm.mp) * b.mp * 5.0 - b.fgm),
        )
        * (1.0 - _d(b.mp, tm_mp5))
    )
    fg_part = b.fgm * (1.0 - 0.5 * _d(b.pts - b.ftm, 2.0 * b.fga) * qast)
    ast_part = (
        0.5 * _d((tm.pts - tm.ftm) - (b.pts - b.ftm), 2.0 * (tm.fga - b.fga)) * b.ast
    )
    ft_part = (1.0 - (1.0 - _d(b.ftm, b.fta)) ** 2) * 0.4 * b.fta if b.fta else 0.0
    tm_scor_poss = tm.fgm + (1.0 - (1.0 - _d(tm.ftm, tm.fta)) ** 2) * tm.fta * 0.4
    team_orb_pct = _d(tm.oreb, tm.oreb + opp.dreb)
    team_play_pct = _d(tm_scor_poss, tm.fga + tm.fta * 0.4 + tm.tov)
    team_orb_weight = _d(
        (1.0 - team_orb_pct) * team_play_pct,
        (1.0 - team_orb_pct) * team_play_pct + team_orb_pct * (1.0 - team_play_pct),
    )
    orb_part = b.oreb * team_orb_weight * team_play_pct

    scposs = (fg_part + ast_part + ft_part) * (
        1.0 - _d(tm.oreb, tm_scor_poss) * team_orb_weight * team_play_pct
    ) + orb_part
    fgx_poss = (b.fga - b.fgm) * (1.0 - 1.07 * team_orb_pct)
    ftx_poss = ((1.0 - _d(b.ftm, b.fta)) ** 2) * 0.4 * b.fta if b.fta else 0.0
    tot_poss = scposs + fgx_poss + ftx_poss + b.tov
    if tot_poss <= 0:
        return 0.0, 0.0, 0.0

    pprod_fg = (
        2.0
        * (b.fgm + 0.5 * b.fg3m)
        * (1.0 - 0.5 * _d(b.pts - b.ftm, 2.0 * b.fga) * qast)
    )
    pprod_ast = (
        2.0
        * _d(tm.fgm - b.fgm + 0.5 * (tm.fg3m - b.fg3m), tm.fgm - b.fgm)
        * 0.5
        * _d((tm.pts - tm.ftm) - (b.pts - b.ftm), 2.0 * (tm.fga - b.fga))
        * b.ast
    )
    pprod_orb = (
        b.oreb
        * team_orb_weight
        * team_play_pct
        * _d(tm.pts, tm.fgm + (1.0 - (1.0 - _d(tm.ftm, tm.fta)) ** 2) * 0.4 * tm.fta)
    )
    pprod = (pprod_fg + pprod_ast + b.ftm) * (
        1.0 - _d(tm.oreb, tm_scor_poss) * team_orb_weight * team_play_pct
    ) + pprod_orb
    return 100.0 * _d(pprod, tot_poss), tot_poss, pprod


def compute_drtg(b: Box, tm: Box, opp: Box, tm_poss: float) -> float:
    """Dean Oliver individual defensive rating."""
    if b.mp <= 0 or tm.mp <= 0:
        return 0.0
    dor_pct = _d(opp.oreb, opp.oreb + tm.dreb)
    dfg_pct = _d(opp.fgm, opp.fga)
    fmwt = _d(
        dfg_pct * (1.0 - dor_pct),
        dfg_pct * (1.0 - dor_pct) + (1.0 - dfg_pct) * dor_pct,
    )
    stops1 = b.stl + b.blk * fmwt * (1.0 - 1.07 * dor_pct) + b.dreb * (1.0 - fmwt)
    stops2 = (
        _d(opp.fga - opp.fgm - tm.blk, tm.mp) * fmwt * (1.0 - 1.07 * dor_pct)
        + _d(opp.tov - tm.stl, tm.mp)
    ) * b.mp + _d(b.pf, tm.pf) * 0.4 * opp.fta * (1.0 - _d(opp.ftm, opp.fta)) ** 2
    stops = stops1 + stops2
    stop_pct = _d(stops * tm.mp, tm_poss * b.mp) if tm_poss else 0.0
    team_drtg = 100.0 * _d(opp.pts, tm_poss) if tm_poss else 0.0
    d_pts_per_scposs = _d(
        opp.pts, opp.fgm + (1.0 - (1.0 - _d(opp.ftm, opp.fta)) ** 2) * opp.fta * 0.4
    )
    return team_drtg + 0.2 * (100.0 * d_pts_per_scposs * (1.0 - stop_pct) - team_drtg)


def compute_metrics(ps: PlayerSeason, ctx: LeagueContext, ws_ppw_coeff: float) -> None:
    """Populate ``ps.metrics`` with the full box-derived basket.

    PER is left un-standardized (``ps.aper``); the caller standardizes per pool.
    BPM/OBPM/DBPM/VORP are filled later by :func:`apply_sl_bpm`.
    """
    b, tm, opp = ps.box, ps.team, ps.opp
    m = ps.metrics
    gp = max(1, b.gp)

    # Shooting / four-factors (player-only).
    m["gmsc"] = round(game_score(b) / gp, 1)
    m["ts_pct"] = round(100.0 * _d(b.pts, 2.0 * (b.fga + 0.44 * b.fta)), 1)
    m["efg_pct"] = round(100.0 * _d(b.fgm + 0.5 * b.fg3m, b.fga), 1)
    m["fg3ar"] = round(_d(b.fg3a, b.fga), 3)
    m["ftr"] = round(_d(b.fta, b.fga), 3)

    # Possession-based rates (pace, pts/100) are raw box-derived measures — not
    # league-relative / pool-calibrated composites — so populate them for every
    # pool, including sub-threshold ones, so per-100 works outside adv_eligible
    # pools (issue #473). They depend only on box totals, not league context.
    tm_mp5 = _d(tm.mp, 5.0)
    tm_poss = tm.poss(opp)
    opp_poss = opp.poss(tm)
    tm_pace = 48.0 * _d(tm_poss + opp_poss, 2.0 * tm_mp5)
    player_poss = tm_poss * _d(b.mp, tm_mp5)
    ps.player_poss = player_poss
    ps.pct_min = _d(b.mp, tm_mp5)
    # Only emit possession rates when the *pool* has real possession data. Pools
    # reconstructed from season logs without team box (mainly 2012-2016) carry
    # skeletal team rows, so their league pace computes ~0 and per-player pace/
    # per-100 are degenerate (explosive per-100). Gate on the pool-level league
    # pace — robust across the pool, unlike a per-row check — so a genuine pool
    # (incl. pre-2017 years that do have box data, e.g. 2007-2010) emits and a
    # skeletal one leaves NULL (per-100 renders blank, as it did before #473).
    if ctx.pace >= MIN_POOL_PACE and tm_poss > 0:
        m["pace"] = round(tm_pace, 1)
        m["pts_per100"] = round(100.0 * _d(b.pts, player_poss), 1)
    else:
        m["pace"] = None
        m["pts_per100"] = None

    # Player/team-box rates use only this player's, team's, and opponent's box
    # totals. They remain meaningful when the *league* pool is incomplete (for
    # example, the NBA advanced feed can be available before team-minute fields
    # meet the stricter league-calibration gate), so do not hide them behind
    # ``adv_eligible``. This keeps the Class Tracker aligned with the Explorer
    # and player surfaces during an in-progress Summer League.
    m["tov_pct"] = round(100.0 * _d(b.tov, b.fga + 0.44 * b.fta + b.tov), 1)
    computed_usg_pct = round(
        100.0
        * _d(
            (b.fga + 0.44 * b.fta + b.tov) * tm_mp5,
            b.mp * (tm.fga + 0.44 * tm.fta + tm.tov),
        ),
        1,
    )
    computed_ast_pct = round(100.0 * _d(b.ast, _d(b.mp, tm_mp5) * tm.fgm - b.fgm), 1)
    m["orb_pct"] = round(100.0 * _d(b.oreb * tm_mp5, b.mp * (tm.oreb + opp.dreb)), 1)
    m["drb_pct"] = round(100.0 * _d(b.dreb * tm_mp5, b.mp * (tm.dreb + opp.oreb)), 1)
    computed_trb_pct = round(100.0 * _d(b.reb * tm_mp5, b.mp * (tm.reb + opp.reb)), 1)
    m["stl_pct"] = round(100.0 * _d(b.stl * tm_mp5, b.mp * opp_poss), 1)
    m["blk_pct"] = round(100.0 * _d(b.blk * tm_mp5, b.mp * (opp.fga - opp.fg3a)), 1)
    # The NBA player Advanced feed is authoritative for these values. Fall
    # back to the equivalent box calculation for historical rows or sources
    # that do not supply the advanced endpoint.
    source_usg_pct = ps.source_rates.get("usg_pct")
    source_ast_pct = ps.source_rates.get("ast_pct")
    source_trb_pct = ps.source_rates.get("trb_pct")
    m["usg_pct"] = source_usg_pct if source_usg_pct is not None else computed_usg_pct
    m["ast_pct"] = source_ast_pct if source_ast_pct is not None else computed_ast_pct
    m["trb_pct"] = source_trb_pct if source_trb_pct is not None else computed_trb_pct

    # League-relative / pool-calibrated metrics only when the pool is eligible.
    if not ctx.adv_eligible:
        for k in (
            "per",
            "ortg",
            "drtg",
            "net",
            "ows",
            "dws",
            "ws",
            "ws40",
            "ws82",
        ):
            m[k] = None
        ps.aper = None
        return

    uper = compute_uper(b, tm, ctx)
    ps.aper = _d(ctx.pace, tm_pace) * uper

    ortg, tot_poss, pprod = compute_ortg(b, tm, opp)
    drtg = compute_drtg(b, tm, opp, tm_poss)
    m["ortg"] = round(ortg, 1)
    m["drtg"] = round(drtg, 1)
    m["net"] = round(ortg - drtg, 1)

    mppw = ws_ppw_coeff * ctx.ppg * _d(tm_pace, ctx.pace)
    marg_off = pprod - 0.92 * ctx.pts_per_poss * tot_poss
    marg_def = _d(b.mp, tm.mp) * tm_poss * (1.08 * ctx.pts_per_poss - drtg / 100.0)
    ows, dws = _d(marg_off, mppw), _d(marg_def, mppw)
    m["ows"] = round(ows, 2)
    m["dws"] = round(dws, 2)
    m["ws"] = round(ows + dws, 2)
    m["ws40"] = round(40.0 * _d(ows + dws, b.mp), 3)
    # Full-season projection: scale the accumulated WS by an 82-game / 48-min
    # "season" relative to the minutes actually available here. ``k82`` ~= 82/G.
    k82 = _d(48.0 * 82.0, tm_mp5)
    m["ws82"] = round((ows + dws) * k82, 2)


# --------------------------------------------------------------------------- #
# SL-native coefficient fits
# --------------------------------------------------------------------------- #
def fit_pythagorean(
    records: dict[int, dict], team_comp: dict[int, int], adv_pools: set[int]
) -> tuple[float, int]:
    """Fit the SL Pythagorean exponent x in ln(W/L) = x·ln(PF/PA).

    Regression through the origin over decided team records in ADV pools. Falls
    back to an NBA-ish 13.0 when too few records exist.
    """
    sxy = sxx = 0.0
    n = 0
    for entry, rec in records.items():
        if team_comp.get(entry) not in adv_pools:
            continue
        w, losses, pf, pa = rec["w"], rec["l"], rec["pf"], rec["pa"]
        if w > 0 and losses > 0 and pf > 0 and pa > 0:
            lx, ly = math.log(pf / pa), math.log(w / losses)
            sxy += lx * ly
            sxx += lx * lx
            n += 1
    if n < 20 or sxx == 0:
        return 13.0, n
    return sxy / sxx, n


def _bpm_feature_row(ps: PlayerSeason) -> Optional[list[float]]:
    """Per-100-possession predictor vector for one player-season, or None."""
    poss = ps.player_poss
    if poss <= 0:
        return None
    b = ps.box
    x = 100.0 / poss
    return [
        (b.fgm - b.fg3m) * x,
        b.fg3m * x,
        b.ftm * x,
        (b.fga - b.fgm) * x,
        (b.fta - b.ftm) * x,
        b.oreb * x,
        b.dreb * x,
        b.ast * x,
        b.stl * x,
        b.blk * x,
        b.tov * x,
        b.pf * x,
    ]


def fit_sl_bpm(
    seasons: list[PlayerSeason],
    adv_pools: set[int],
    min_mp: float = QUALIFY_MIN_MINUTES,
) -> tuple[Optional[dict[str, float]], float, float, int]:
    """Weighted OLS of per-100 box stats onto per-100 plus-minus over SL pools.

    Returns ``(coef, intercept, weighted_r2, n)``; ``coef`` is ``None`` when too
    few rows exist to fit.
    """
    import numpy as np

    rows, targets, weights = [], [], []
    for ps in seasons:
        if ps.competition_id not in adv_pools or ps.box.mp < min_mp:
            continue
        feats = _bpm_feature_row(ps)
        if feats is None or ps.player_poss <= 0:
            continue
        rows.append(feats)
        targets.append(ps.pm * 100.0 / ps.player_poss)
        weights.append(ps.box.mp)

    n = len(rows)
    if n < 50:
        return None, 0.0, 0.0, n

    x = np.array(rows, dtype=float)
    y = np.array(targets, dtype=float)
    w = np.array(weights, dtype=float)
    x_aug = np.hstack([x, np.ones((n, 1))])
    sw = np.sqrt(w)
    beta, *_ = np.linalg.lstsq(x_aug * sw[:, None], y * sw, rcond=None)

    pred = x_aug @ beta
    ybar = float(np.average(y, weights=w))
    ss_res = float(np.sum(w * (y - pred) ** 2))
    ss_tot = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
    coef = {f: float(beta[i]) for i, f in enumerate(BPM_FEATURES)}
    return coef, float(beta[-1]), r2, n


def apply_sl_bpm(
    seasons: list[PlayerSeason],
    by_pool: dict[int, list[PlayerSeason]],
    coef: Optional[dict[str, float]],
    intercept: float,
) -> None:
    """Assign raw BPM + offense/defense parts, re-center per pool, then VORP.

    OBPM/DBPM are the offensive- and defensive-feature contributions of the
    fitted model, each centered to its pool mean; (OBPM + DBPM) == BPM exactly.
    """
    for ps in seasons:
        feats = _bpm_feature_row(ps)
        if feats is None or coef is None:
            ps.metrics["bpm"] = ps.metrics["obpm"] = ps.metrics["dbpm"] = None
            ps.metrics["vorp"] = ps.metrics["vorp82"] = None
            continue
        ps.raw_off = sum(
            coef[f] * v for f, v in zip(BPM_FEATURES, feats) if f in OFF_FEATURES
        )
        ps.raw_def = sum(
            coef[f] * v for f, v in zip(BPM_FEATURES, feats) if f in DEF_FEATURES
        )
        ps.raw_bpm = intercept + ps.raw_off + ps.raw_def

    for pool in by_pool.values():
        scored = [ps for ps in pool if ps.raw_bpm is not None]
        if not scored:
            continue
        wmp = sum(ps.box.mp for ps in scored) or 1.0
        off_mean = sum((ps.raw_off or 0.0) * ps.box.mp for ps in scored) / wmp
        def_mean = sum((ps.raw_def or 0.0) * ps.box.mp for ps in scored) / wmp
        for ps in scored:
            obpm = (ps.raw_off or 0.0) - off_mean
            dbpm = (ps.raw_def or 0.0) - def_mean
            bpm = obpm + dbpm
            ps.metrics["obpm"] = round(obpm, 1)
            ps.metrics["dbpm"] = round(dbpm, 1)
            ps.metrics["bpm"] = round(bpm, 1)
            # Cumulative VORP: points-above-replacement → wins, accrued over the
            # minutes actually played (standard BBRef identity MP/(48*82)). Small
            # for a few-game SL sample, and additive across competitions.
            ps.metrics["vorp"] = round(
                (bpm - VORP_REPLACEMENT) * ps.box.mp / (48.0 * 82.0), 2
            )
            # VORP/82: the same rate projected to a full 82-game season
            # (``pct_min`` is the share of available lineup-minutes). This is the
            # prior bare-"VORP" value, now labelled as the per-season pace.
            ps.metrics["vorp82"] = round((bpm - VORP_REPLACEMENT) * ps.pct_min, 2)


# --------------------------------------------------------------------------- #
# Loading + assembly
# --------------------------------------------------------------------------- #
@dataclass
class ComputeResult:
    """Everything a rebuild needs to persist."""

    contexts: dict[int, LeagueContext]
    seasons: list[PlayerSeason]
    pyth_exponent: float
    pyth_n: int
    ws_ppw_coeff: float
    bpm_coef: Optional[dict[str, float]]
    bpm_intercept: float
    bpm_r2: float
    bpm_n_fit: int
    # Shot-diet zone FGA per (player_id, competition_id); empty dict when no
    # shot-chart data exists (no SummerLeagueShotEvent rows ingested yet).
    shot_diet: dict[tuple[int, int], dict[str, int]] = field(default_factory=dict)
    # Assisted-FG counts per (player_id, competition_id) from PBP made-FG events;
    # value is (ast_fgm, unast_fgm). Empty when no PBP data has been ingested.
    assisted_fg: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)


async def _load_shot_diet(
    db: AsyncSession,
) -> dict[tuple[int, int], dict[str, int]]:
    """Query shot-zone FGA counts aggregated per (player_id, competition_id).

    Excludes backcourt shots and rows with NULL player_id (unresolved).
    Returns a mapping of ``(player_id, competition_id)`` → zone → FGA count.
    """
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeagueShotEvent.player_id,
            SummerLeagueShotEvent.competition_id,
            SummerLeagueShotEvent.shot_zone_basic,
            func.count().label("fga"),
        )
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeagueShotEvent.game_id,  # type: ignore[arg-type]
        )
        .where(SummerLeagueShotEvent.player_id.isnot(None))  # type: ignore[union-attr]
        .where(SummerLeagueShotEvent.shot_zone_basic.isnot(None))  # type: ignore[union-attr]
        .where(_season_game_status_clause())
        .where(
            SummerLeagueShotEvent.shot_zone_basic.notin_(list(_EXCLUDED_ZONES))  # type: ignore[union-attr]
        )
        .group_by(
            SummerLeagueShotEvent.player_id,
            SummerLeagueShotEvent.competition_id,
            SummerLeagueShotEvent.shot_zone_basic,
        )
    )
    rows = (await db.execute(stmt)).all()
    out: dict[tuple[int, int], dict[str, int]] = defaultdict(dict)
    for player_id, competition_id, zone, fga in rows:
        out[(int(player_id), int(competition_id))][str(zone)] = int(fga)
    return out


async def _load_assisted_fg(
    db: AsyncSession,
) -> dict[tuple[int, int], tuple[int, int]]:
    """Query PBP made-FG events and count assisted vs unassisted per (player_id, competition_id).

    A made-FG event is identified by ``event_msg_type == 1``.  An assisted made
    FG is one where an assister was recorded — keyed on the raw
    ``person2_nba_id`` (present whenever the box notes an assist) rather than the
    canonical ``person2_id``, which is NULL when the assister isn't yet
    resolved to our player table and would otherwise undercount assists on
    partially-resolved data.  Only events with a resolved scorer
    (``person1_id`` not NULL) are counted.

    Returns a mapping of ``(player_id, competition_id)`` → ``(ast_fgm, unast_fgm)``.
    The mapping is empty when no PBP made-FG data has been ingested.
    """
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeaguePlayByPlayEvent.person1_id,
            SummerLeaguePlayByPlayEvent.competition_id,
            func.count()
            .filter(SummerLeaguePlayByPlayEvent.person2_nba_id.isnot(None))  # type: ignore[union-attr]
            .label("ast_fgm"),
            func.count()
            .filter(SummerLeaguePlayByPlayEvent.person2_nba_id.is_(None))  # type: ignore[union-attr]
            .label("unast_fgm"),
        )
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeaguePlayByPlayEvent.game_id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayByPlayEvent.event_msg_type == 1,  # type: ignore[arg-type]
            SummerLeaguePlayByPlayEvent.person1_id.isnot(None),  # type: ignore[union-attr]
            _season_game_status_clause(),
        )
        .group_by(
            SummerLeaguePlayByPlayEvent.person1_id,
            SummerLeaguePlayByPlayEvent.competition_id,
        )
    )
    rows = (await db.execute(stmt)).all()
    return {
        (int(player_id), int(competition_id)): (int(ast_fgm), int(unast_fgm))
        for player_id, competition_id, ast_fgm, unast_fgm in rows
    }


async def _load(db: AsyncSession) -> tuple[Any, ...]:
    comp = SummerLeagueCompetition
    tgl = SummerLeagueTeamGameLog
    pgl = SummerLeaguePlayerGameLog

    comps = {
        c.id: (c.year, c.venue_slug) for c in (await db.execute(select(comp))).scalars()
    }
    games = {
        g.id: (g.home_team_entry_id, g.away_team_entry_id, g.home_score, g.away_score)
        for g in (
            await db.execute(
                select(SummerLeagueGame).where(_season_game_status_clause())
            )
        ).scalars()
    }
    team_rows = (
        (
            await db.execute(
                select(tgl)
                .join(
                    SummerLeagueGame,
                    SummerLeagueGame.id == tgl.game_id,  # type: ignore[arg-type]
                )
                .where(_season_game_status_clause())
            )
        )
        .scalars()
        .all()
    )
    team_mp = {
        tid: (sec or 0) / 60.0
        for tid, sec in (
            await db.execute(
                select(pgl.team_entry_id, func.sum(pgl.minutes_seconds))  # type: ignore[call-overload]
                .join(
                    SummerLeagueGame,
                    SummerLeagueGame.id == pgl.game_id,  # type: ignore[arg-type]
                )
                .where(_season_game_status_clause())
                .group_by(pgl.team_entry_id)
            )
        ).all()
    }
    sec: Any = pgl.minutes_seconds  # column expression for arithmetic/compare

    def _source_rate_sum(metric: str) -> Any:
        """Minute-weighted numerator for one NBA player Advanced rate."""
        column = getattr(pgl, _SOURCE_RATE_COLUMNS[metric])
        return func.sum(column * sec).label(f"{metric}_weighted")

    def _source_rate_seconds(metric: str) -> Any:
        """Eligible minutes for one NBA player Advanced rate."""
        column = getattr(pgl, _SOURCE_RATE_COLUMNS[metric])
        return func.sum(case((column.isnot(None), sec), else_=0)).label(
            f"{metric}_seconds"
        )

    pgl_player_id: Any = pgl.player_id
    player_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                pgl.competition_id,
                pgl.player_id,
                pgl.team_entry_id,
                func.count().label("gp"),
                func.sum(sec).label("sec"),
                *[func.sum(getattr(pgl, f)).label(f) for f in _BOX_INT_FIELDS],
                func.sum(pgl.plus_minus).label("plus_minus"),
                *[_source_rate_sum(metric) for metric in _SOURCE_RATE_COLUMNS],
                *[_source_rate_seconds(metric) for metric in _SOURCE_RATE_COLUMNS],
            )
            .join(
                SummerLeagueGame,
                SummerLeagueGame.id == pgl.game_id,  # type: ignore[arg-type]
            )
            .join(PlayerMaster, PlayerMaster.id == pgl.player_id)
            .where(
                pgl_player_id.isnot(None),
                sec > 0,
                _season_game_status_clause(),
            )
            .group_by(pgl.competition_id, pgl.player_id, pgl.team_entry_id)
        )
    ).all()
    return comps, games, team_rows, team_mp, player_rows


def _build(comps, games, team_rows, team_mp):
    team_box: dict[int, Box] = defaultdict(Box)
    team_comp: dict[int, int] = {}
    opp_box: dict[int, Box] = defaultdict(Box)
    row_by_game_entry: dict[tuple[int, int], Any] = {}

    for r in team_rows:
        team_box[r.team_entry_id].add_row(r)
        team_comp[r.team_entry_id] = r.competition_id
        row_by_game_entry[(r.game_id, r.team_entry_id)] = r

    records: dict[int, dict] = defaultdict(lambda: {"w": 0, "l": 0, "pf": 0, "pa": 0})
    for gid, (home, away, hs, as_) in games.items():
        for me, other, my_s, opp_s in ((home, away, hs, as_), (away, home, as_, hs)):
            if me is None or other is None:
                continue
            other_row = row_by_game_entry.get((gid, other))
            if other_row is not None:
                opp_box[me].add_row(other_row)
            if my_s is not None and opp_s is not None:
                rec = records[me]
                rec["pf"] += my_s
                rec["pa"] += opp_s
                if my_s > opp_s:
                    rec["w"] += 1
                elif my_s < opp_s:
                    rec["l"] += 1

    for entry, box in team_box.items():
        box.mp = team_mp.get(entry, 0.0)

    lg_box: dict[int, Box] = defaultdict(Box)
    lg_team_games: dict[int, int] = defaultdict(int)
    for (_gid, _entry), row in row_by_game_entry.items():
        lg_box[row.competition_id].add_row(row)
        lg_team_games[row.competition_id] += 1
    lg_poss: dict[int, float] = defaultdict(float)
    lg_mp: dict[int, float] = defaultdict(float)
    for entry, box in team_box.items():
        cid = team_comp[entry]
        lg_poss[cid] += box.poss(opp_box[entry])
        lg_mp[cid] += box.mp

    complete = _completeness(team_rows)

    contexts: dict[int, LeagueContext] = {}
    for cid, box in lg_box.items():
        year, venue = comps[cid]
        box.mp = lg_mp[cid]
        cov = complete.get(cid, {"games": 0, "complete": 0})
        ctx = LeagueContext(
            competition_id=cid,
            year=year,
            venue=venue,
            lg=box,
            poss=lg_poss[cid],
            team_games=lg_team_games[cid],
            total_games=cov["games"],
            complete_games=cov["complete"],
        )
        ctx.finalize()
        contexts[cid] = ctx

    return team_box, opp_box, team_comp, contexts, records


def _completeness(team_rows) -> dict[int, dict]:
    """Per-competition game counts: total games and complete-box games.

    A game is complete when both team rows exist and each team's *per-game* box
    minutes are in a sane regulation range (filters broken partial boxes). Uses
    the team row's own ``minutes`` — not a season total — so a single partial
    game can't be masked by the team's other (complete) games.
    """
    by_game: dict[int, list] = defaultdict(list)
    game_comp: dict[int, int] = {}
    for r in team_rows:
        by_game[r.game_id].append(r)
        game_comp[r.game_id] = r.competition_id
    out: dict[int, dict] = defaultdict(lambda: {"games": 0, "complete": 0})
    for gid, rows in by_game.items():
        cid = game_comp[gid]
        out[cid]["games"] += 1
        if len(rows) == 2 and all(
            (r.minutes or 0) >= MIN_COMPLETE_TEAM_MP for r in rows
        ):
            out[cid]["complete"] += 1
    return out


async def compute(db: AsyncSession) -> ComputeResult:
    """Load raw logs and compute every metric in memory."""
    comps, games, team_rows, team_mp, player_rows = await _load(db)
    shot_diet = await _load_shot_diet(db)
    assisted_fg = await _load_assisted_fg(db)
    team_box, opp_box, team_comp, contexts, records = _build(
        comps, games, team_rows, team_mp
    )

    # Merge multiple team-entries per (comp, player); primary = most minutes.
    merged: dict[tuple[int, int], dict] = {}
    for r in player_rows:
        key = (r.competition_id, r.player_id)
        sec = float(r.sec or 0)
        cur = merged.get(key)
        if cur is None:
            cur = {
                "box": Box(),
                "entry": r.team_entry_id,
                "sec": sec,
                "pm": 0.0,
                "source_rate_weighted": defaultdict(float),
                "source_rate_seconds": defaultdict(float),
            }
            merged[key] = cur
        bx = cur["box"]
        bx.gp += int(r.gp)
        bx.mp += sec / 60.0
        cur["pm"] += float(r.plus_minus or 0)
        for f in _BOX_INT_FIELDS:
            setattr(bx, f, getattr(bx, f) + float(getattr(r, f) or 0))
        for metric in _SOURCE_RATE_COLUMNS:
            cur["source_rate_weighted"][metric] += float(
                getattr(r, f"{metric}_weighted") or 0
            )
            cur["source_rate_seconds"][metric] += float(
                getattr(r, f"{metric}_seconds") or 0
            )
        if sec > cur["sec"]:
            cur["entry"] = r.team_entry_id
            cur["sec"] = sec

    seasons: list[PlayerSeason] = []
    for (cid, pid), v in merged.items():
        year, venue = comps[cid]
        entry = v["entry"]
        source_rates = {
            metric: (
                round(100.0 * v["source_rate_weighted"][metric] / denominator, 1)
                if (denominator := v["source_rate_seconds"][metric])
                else None
            )
            for metric in _SOURCE_RATE_COLUMNS
        }
        seasons.append(
            PlayerSeason(
                player_id=pid,
                competition_id=cid,
                primary_team_entry_id=entry,
                year=year,
                venue=venue,
                box=v["box"],
                team=team_box[entry],
                opp=opp_box[entry],
                pm=v["pm"],
                source_rates=source_rates,
            )
        )

    by_pool: dict[int, list[PlayerSeason]] = defaultdict(list)
    for ps in seasons:
        by_pool[ps.competition_id].append(ps)

    # Gate pools for league-relative metrics.
    adv_pools: set[int] = set()
    for cid, pool in by_pool.items():
        qual = sum(1 for p in pool if p.box.mp >= QUALIFY_MIN_MINUTES)
        ctx = contexts[cid]
        cfrac = _d(ctx.complete_games, ctx.total_games)
        if cfrac >= ADV_MIN_COMPLETE_FRAC and qual >= ADV_MIN_PLAYERS:
            adv_pools.add(cid)
            ctx.adv_eligible = True

    # (a) SL points-to-wins from the Pythagorean exponent.
    pyth_x, pyth_n = fit_pythagorean(records, team_comp, adv_pools)
    ws_ppw_coeff = 4.0 / pyth_x

    for ps in seasons:
        compute_metrics(ps, contexts[ps.competition_id], ws_ppw_coeff)

    # Standardize PER per pool so the minute-weighted mean aPER -> 15.
    for cid, pool in by_pool.items():
        if not contexts[cid].adv_eligible:
            continue
        num = sum((ps.aper or 0.0) * ps.box.mp for ps in pool)
        den = sum(ps.box.mp for ps in pool)
        scalar = _d(num, den)
        contexts[cid].aper_scalar = scalar
        for ps in pool:
            ps.metrics["per"] = (
                round(ps.aper * _d(15.0, scalar), 1) if ps.aper is not None else None
            )

    # (b) SL-native BPM (+ OBPM/DBPM/VORP); fit only over adv pools.
    adv_by_pool = {cid: pool for cid, pool in by_pool.items() if cid in adv_pools}
    coef, intercept, r2, n_fit = fit_sl_bpm(seasons, adv_pools)
    apply_sl_bpm(seasons, adv_by_pool, coef, intercept)

    return ComputeResult(
        contexts=contexts,
        seasons=seasons,
        pyth_exponent=pyth_x,
        pyth_n=pyth_n,
        ws_ppw_coeff=ws_ppw_coeff,
        bpm_coef=coef,
        bpm_intercept=intercept,
        bpm_r2=r2,
        bpm_n_fit=n_fit,
        shot_diet=shot_diet,
        assisted_fg=assisted_fg,
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _season_columns(
    ps: PlayerSeason,
    model_version: str,
    adv_eligible: bool,
    zone_fga: dict[str, int],
    pbp_counts: tuple[int, int] | None = None,
) -> dict[str, Any]:
    b, m = ps.box, ps.metrics
    diet = compute_shot_diet(zone_fga)
    ast_fgm, unast_fgm = pbp_counts if pbp_counts is not None else (None, None)
    return {
        "competition_id": ps.competition_id,
        "player_id": ps.player_id,
        "primary_team_entry_id": ps.primary_team_entry_id,
        "year": ps.year,
        "venue_slug": ps.venue,
        "gp": b.gp,
        "minutes": round(b.mp, 1),
        **{f: int(getattr(b, f)) for f in _BOX_INT_FIELDS},
        "plus_minus": int(ps.pm),
        "ts_pct": m.get("ts_pct"),
        "efg_pct": m.get("efg_pct"),
        "fg3ar": m.get("fg3ar"),
        "ftr": m.get("ftr"),
        "gmsc": m.get("gmsc"),
        "usg_pct": m.get("usg_pct"),
        "ast_pct": m.get("ast_pct"),
        "orb_pct": m.get("orb_pct"),
        "drb_pct": m.get("drb_pct"),
        "trb_pct": m.get("trb_pct"),
        "stl_pct": m.get("stl_pct"),
        "blk_pct": m.get("blk_pct"),
        "tov_pct": m.get("tov_pct"),
        "pace": m.get("pace"),
        "pts_per100": m.get("pts_per100"),
        "per": m.get("per"),
        "ortg": m.get("ortg"),
        "drtg": m.get("drtg"),
        "net_rtg": m.get("net"),
        "ows": m.get("ows"),
        "dws": m.get("dws"),
        "ws": m.get("ws"),
        "ws40": m.get("ws40"),
        "ws82": m.get("ws82"),
        "obpm": m.get("obpm"),
        "dbpm": m.get("dbpm"),
        "bpm": m.get("bpm"),
        "vorp": m.get("vorp"),
        "vorp82": m.get("vorp82"),
        "rim_rate": diet["rim_rate"],
        "mid_rate": diet["mid_rate"],
        "three_rate": diet["three_rate"],
        "corner3_rate": diet["corner3_rate"],
        "ast_fgm": ast_fgm,
        "unast_fgm": unast_fgm,
        "adv_eligible": adv_eligible,
        "model_version": model_version,
    }


async def _active_or_fresh_model_version(db: AsyncSession) -> str:
    """The currently active model's version stamp, or a freshly minted one.

    A scoped :func:`rebuild` call never writes a new
    :class:`SummerLeagueMetricModel` row (see that function's docstring),
    but ``SummerLeaguePlayerSeason.model_version`` is still a useful
    informational stamp (no FK relies on it) -- reuse whichever model is
    active so a scoped tick's season rows reference the same fit the last
    full rebuild wrote, falling back to a freshly minted version only when
    no full rebuild has ever run yet (nothing to reference).
    """
    stmt = (
        select(SummerLeagueMetricModel.model_version)  # type: ignore[call-overload]
        .where(SummerLeagueMetricModel.is_active.is_(True))  # type: ignore[attr-defined]
        .order_by(SummerLeagueMetricModel.id.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is not None:
        return str(row[0])
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


async def rebuild(
    db: AsyncSession,
    *,
    model_version: Optional[str] = None,
    competition_ids: Optional[Sequence[int]] = None,
) -> dict[str, int]:
    """Recompute materialized SL metric tables -- full rebuild, or scoped by competition.

    Two modes, selected by ``competition_ids``:

    * **Unscoped** (default, ``competition_ids=None``) -- a full
      wipe-and-rebuild, byte-for-byte the same as before #523: every
      ``summer_league_player_seasons`` / ``_metric_contexts`` /
      ``_metric_models`` row is deleted and replaced, including a freshly
      minted, freshly fitted global model row. This is what
      ``scripts/rebuild_sl_metrics.py`` (the offline full recompute) calls.
    * **Scoped** (``competition_ids`` a sequence of competition ids) -- only
      ``SummerLeaguePlayerSeason`` / ``SummerLeagueMetricContext`` rows for
      those competition ids are deleted and replaced; every other
      competition's materialized rows -- including rows this function never
      wrote at all -- are left untouched. The global
      ``SummerLeagueMetricModel`` fit is never written on a scoped call: it
      isn't scoped data, and refitting the league-wide Pythagorean/BPM
      coefficients from an hourly per-competition tick would spam model
      versions for no benefit. Scoped season rows are instead stamped with
      :func:`_active_or_fresh_model_version`. An empty ``competition_ids``
      sequence is a safe no-op (nothing loaded, deleted, or written) --
      mirrors "nothing new to normalize this tick".

    :func:`compute` always loads and fits over the *entire* raw dataset
    regardless of scope: the league-relative recalibration constants and the
    SL-native Pythagorean/BPM fits are only correct when derived from the
    full pool, never a truncated one. Scope only narrows what gets
    *persisted* -- which is what makes a scoped call safe to run hourly
    without either an expensive/incorrect truncated fit or a destructive
    wipe of data outside its scope.

    Returns a small summary dict (counts of what was actually written). The
    caller controls the transaction.
    """
    if competition_ids is not None and not competition_ids:
        return {"seasons": 0, "contexts": 0, "adv_pools": 0}

    result = await compute(db)
    adv_cids = {cid for cid, ctx in result.contexts.items() if ctx.adv_eligible}
    scope = frozenset(competition_ids) if competition_ids is not None else None

    if scope is None:
        # Unscoped: full wipe-and-rebuild, unchanged from before #523. P2 debt, not an
        # accepted pattern — Phase 1's version-flip publish replaces it and removes these
        # waivers. See docs/plans/summer-league-remediation-roadmap.md.
        # discipline: unscoped-delete P2 debt, removed by the Phase 1 version-flip
        await db.execute(delete(SummerLeaguePlayerSeason))
        # discipline: unscoped-delete P2 debt, removed by the Phase 1 version-flip
        await db.execute(delete(SummerLeagueMetricContext))
        # discipline: unscoped-delete P2 debt, removed by the Phase 1 version-flip
        await db.execute(delete(SummerLeagueMetricModel))

        version = model_version or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        db.add(
            SummerLeagueMetricModel(
                model_version=version,
                pyth_exponent=result.pyth_exponent,
                ws_ppw_coeff=result.ws_ppw_coeff,
                pyth_n_teams=result.pyth_n,
                bpm_intercept=result.bpm_intercept,
                bpm_r2=result.bpm_r2,
                bpm_n_fit=result.bpm_n_fit,
                bpm_replacement=VORP_REPLACEMENT,
                bpm_coefficients=result.bpm_coef or {},
            )
        )
        contexts_to_write = list(result.contexts.values())
        seasons_to_write = result.seasons
    else:
        # Scoped: delete + write only rows for the given competition ids;
        # never touch the global model row (see docstring above).
        await db.execute(
            delete(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.competition_id.in_(scope)  # type: ignore[attr-defined]
            )
        )
        await db.execute(
            delete(SummerLeagueMetricContext).where(
                SummerLeagueMetricContext.competition_id.in_(scope)  # type: ignore[attr-defined]
            )
        )
        version = model_version or await _active_or_fresh_model_version(db)
        contexts_to_write = [
            ctx for cid, ctx in result.contexts.items() if cid in scope
        ]
        seasons_to_write = [ps for ps in result.seasons if ps.competition_id in scope]

    for ctx in contexts_to_write:
        db.add(
            SummerLeagueMetricContext(
                competition_id=ctx.competition_id,
                year=ctx.year,
                venue_slug=ctx.venue,
                pace=round(ctx.pace, 3),
                pts_per_poss=round(ctx.pts_per_poss, 4),
                ppg=round(ctx.ppg, 3),
                factor=round(ctx.factor, 4),
                vop=round(ctx.vop, 4),
                drb_pct=round(ctx.drb_pct, 4),
                aper_scalar=round(ctx.aper_scalar, 4),
                n_team_games=ctx.team_games,
                n_complete_games=ctx.complete_games,
                adv_eligible=ctx.adv_eligible,
            )
        )

    n_seasons = 0
    for ps in seasons_to_write:
        zone_fga = result.shot_diet.get((ps.player_id, ps.competition_id), {})
        pbp_counts = result.assisted_fg.get((ps.player_id, ps.competition_id))
        cols = _season_columns(
            ps, version, ps.competition_id in adv_cids, zone_fga, pbp_counts
        )
        db.add(SummerLeaguePlayerSeason(**cols))
        n_seasons += 1

    return {
        "seasons": n_seasons,
        "contexts": len(contexts_to_write),
        "adv_pools": len(adv_cids if scope is None else adv_cids & scope),
    }

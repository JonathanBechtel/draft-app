"""Pure box-derived metric formulas.

Game Score, the line-grain rate helpers, and the Dean Oliver ORtg/DRtg/PER
building blocks :func:`compute_metrics` assembles into the full per-season
basket.

Every function here takes :class:`~app.services.stats.inputs.StatInputs` /
:class:`~app.services.stats.inputs.PoolContext` (or their raw components) and
returns numbers -- no ORM types, no Summer-League-specific vocabulary. See the
package docstring (``app/services/stats/__init__.py``) for the import contract
this depends on.
"""

# discipline: file-size Phase 2 engine extraction; decomposition is tracked outside #745

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from app.services.stats.inputs import PlayerSeason, PoolContext, StatInputs, _d
from app.services.stats.registry import RollupClass, get_metric

# Pool-level pace floor below which the possession estimate is not real. Genuine
# Summer League pools run ~85-115; pools reconstructed from season logs without
# team box data (mainly 2012-2016) compute ~0-4. Anything under this floor has no
# trustworthy possessions, so player pace/pts_per100 are left NULL for that pool.
MIN_POOL_PACE = 40.0

# --------------------------------------------------------------------------- #
# Per-player metric computation
# --------------------------------------------------------------------------- #


def game_score(b: StatInputs) -> float:
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
    Game Score, matching the materialized ``SummerLeaguePlayerSeason.gmsc`` --
    this is precisely what ``app.services.stats.registry``'s ``gmsc`` entry
    declares as ``RollupClass.RECOMBINABLE`` (see the module-level assertion
    below, T8b / #729): recompute from summed box components at the target
    grain rather than average per-grain Game Scores.
    """
    return game_score(
        StatInputs(
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


# T8b (#729): read (not restate) the registry's rollup_class for gmsc, so a
# future re-classification of Game Score in the registry fails this import
# loudly instead of silently drifting from the recombination shortcut above.
assert get_metric("gmsc").rollup_class is RollupClass.RECOMBINABLE, (
    "game_score_line's linear-recombination shortcut requires gmsc to be "
    "declared RollupClass.RECOMBINABLE in app.services.stats.registry"
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


def ftr_ratio(*, fga: Any, fta: Any) -> Optional[float]:
    """Free-throw rate (FTA / FGA), unrounded — the 0–1 fraction, or ``None``.

    Unrounded twin of :func:`ftr_line`, for callers that pool several box
    lines (or several grains) and apply their own display rounding once, at
    the end — e.g. the Explorer's cross-competition ``rollup_recombinable``.
    """
    fga_f = float(fga or 0)
    if fga_f <= 0:
        return None
    return float(fta or 0) / fga_f


def ftr_line(*, fga: Any, fta: Any) -> Optional[float]:
    """Free-throw rate (FTA / FGA) for one box line, ``None``-coalescing to 0.

    Returns:
        The 0–1 fraction rounded to 3 decimals (the scale ``compute_metrics``
        stores), or ``None`` when the line has no field-goal attempts.
    """
    ratio = ftr_ratio(fga=fga, fta=fta)
    return round(ratio, 3) if ratio is not None else None


def tov_pct_ratio(*, fga: Any, fta: Any, tov: Any) -> Optional[float]:
    """Turnover % — 100 · TOV / (FGA + 0.44 · FTA + TOV) — unrounded.

    Unrounded twin of :func:`tov_pct_line`; see :func:`ftr_ratio` for why a
    pooling caller wants the raw value.
    """
    den = float(fga or 0) + 0.44 * float(fta or 0) + float(tov or 0)
    if den <= 0:
        return None
    return 100.0 * float(tov or 0) / den


def tov_pct_line(*, fga: Any, fta: Any, tov: Any) -> Optional[float]:
    """Turnover % — 100 · TOV / (FGA + 0.44 · FTA + TOV) — for one box line.

    Returns:
        The 0–100 percentage rounded to 1 decimal, or ``None`` when the line
        has no true-shooting attempts or turnovers to divide by.
    """
    ratio = tov_pct_ratio(fga=fga, fta=fta, tov=tov)
    return round(ratio, 1) if ratio is not None else None


def ts_pct_ratio(*, pts: Any, fga: Any, fta: Any) -> Optional[float]:
    """True Shooting % — 100 · PTS / (2 · (FGA + 0.44 · FTA)) — unrounded.

    Unrounded twin of :func:`ts_pct_line`; see :func:`ftr_ratio` for why a
    pooling caller wants the raw value.
    """
    denom = 2.0 * (float(fga or 0) + 0.44 * float(fta or 0))
    if denom <= 0:
        return None
    return 100.0 * float(pts or 0) / denom


def ts_pct_line(*, pts: Any, fga: Any, fta: Any) -> Optional[float]:
    """True Shooting % for one box line, ``None``-coalescing to 0.

    Returns:
        The 0–100 percentage rounded to 1 decimal (matches the scale
        ``compute_metrics`` stores), or ``None`` when the line has no
        true-shooting attempts.
    """
    ratio = ts_pct_ratio(pts=pts, fga=fga, fta=fta)
    return round(ratio, 1) if ratio is not None else None


def efg_pct_ratio(*, fgm: Any, fga: Any, fg3m: Any) -> Optional[float]:
    """Effective FG% — 100 · (FGM + 0.5 · FG3M) / FGA — unrounded.

    Unrounded twin of :func:`efg_pct_line`; see :func:`ftr_ratio` for why a
    pooling caller wants the raw value.
    """
    fga_f = float(fga or 0)
    if fga_f <= 0:
        return None
    return 100.0 * (float(fgm or 0) + 0.5 * float(fg3m or 0)) / fga_f


def efg_pct_line(*, fgm: Any, fga: Any, fg3m: Any) -> Optional[float]:
    """Effective FG% for one box line, ``None``-coalescing to 0.

    Returns:
        The 0–100 percentage rounded to 1 decimal (matches the scale
        ``compute_metrics`` stores), or ``None`` when the line has no
        field-goal attempts.
    """
    ratio = efg_pct_ratio(fgm=fgm, fga=fga, fg3m=fg3m)
    return round(ratio, 1) if ratio is not None else None


def fg3ar_ratio(*, fg3a: Any, fga: Any) -> Optional[float]:
    """3-point attempt rate (FG3A / FGA), unrounded — the 0–1 fraction, or ``None``.

    Unrounded twin of :func:`fg3ar_line`; see :func:`ftr_ratio` for why a
    pooling caller wants the raw value.
    """
    fga_f = float(fga or 0)
    if fga_f <= 0:
        return None
    return float(fg3a or 0) / fga_f


def fg3ar_line(*, fg3a: Any, fga: Any) -> Optional[float]:
    """3-point attempt rate (FG3A / FGA) for one box line, ``None``-coalescing to 0.

    Returns:
        The 0–1 fraction rounded to 3 decimals (the scale ``compute_metrics``
        stores), or ``None`` when the line has no field-goal attempts.
    """
    ratio = fg3ar_ratio(fg3a=fg3a, fga=fga)
    return round(ratio, 3) if ratio is not None else None


def astd_pct_ratio(*, ast_fgm: Any, unast_fgm: Any) -> Optional[float]:
    """AST'd% -- share of a player's *own* made FGs that were assisted -- unrounded.

    ``100 * ast_fgm / (ast_fgm + unast_fgm)`` from parsed play-by-play assist
    attribution. Unrounded twin of :func:`astd_pct_line`; see :func:`ftr_ratio`
    for why a pooling caller wants the raw value.

    Not to be confused with :func:`ast_pct_line` (Assist %): that metric is the
    share of *teammates'* field goals this player assisted while on the floor.
    ``astd_pct`` and ``ast_pct`` share a prefix and nothing else -- see
    ``app.services.stats.registry``'s ``astd_pct`` entry for the full
    distinction.

    Returns:
        The 0-100 percentage, unrounded, or ``None`` when the player made no
        field goals to attribute (``ast_fgm + unast_fgm <= 0``).
    """
    ast_fgm_f = float(ast_fgm or 0)
    unast_fgm_f = float(unast_fgm or 0)
    made = ast_fgm_f + unast_fgm_f
    if made <= 0:
        return None
    return 100.0 * ast_fgm_f / made


def astd_pct_line(*, ast_fgm: Any, unast_fgm: Any) -> Optional[float]:
    """AST'd% for one box line (or a caller's own pre-summed rollup), ``None``-coalescing to 0.

    Returns:
        The 0-100 percentage rounded to 1 decimal, or ``None`` when the
        player made no field goals to attribute.
    """
    ratio = astd_pct_ratio(ast_fgm=ast_fgm, unast_fgm=unast_fgm)
    return round(ratio, 1) if ratio is not None else None


def game_advanced_line(
    b: StatInputs,
    tm: Optional[StatInputs] = None,
    opp: Optional[StatInputs] = None,
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


def compute_uper(b: StatInputs, tm: StatInputs, ctx: PoolContext) -> float:
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


def compute_ortg(
    b: StatInputs, tm: StatInputs, opp: StatInputs
) -> tuple[float, float, float]:
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


def compute_drtg(
    b: StatInputs, tm: StatInputs, opp: StatInputs, tm_poss: float
) -> float:
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


def compute_metrics(ps: PlayerSeason, ctx: PoolContext, ws_ppw_coeff: float) -> None:
    """Populate ``ps.metrics`` with the full box-derived basket.

    PER is left un-standardized (``ps.aper``); the caller standardizes per pool.
    BPM/OBPM/DBPM/VORP are filled later by the SL-native fit
    (``app.services.summer_league.metrics.apply_sl_bpm``) -- that fit is
    calibrated against real Summer League plus-minus and stays with its
    orchestration, not in this source-agnostic engine.
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

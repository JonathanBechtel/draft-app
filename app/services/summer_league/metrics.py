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

The pure formulas and neutral input dataclasses (``Box``, ``LeagueContext``,
``compute_metrics``, ``game_score``, ...) now live in ``app.services.stats``
(Phase 2, #722) and are re-exported here under their historical names so every
caller of this module keeps working unchanged. What stays here is everything
DB-bound: loading raw logs, the SL-native Pythagorean/BPM coefficient fits, and
persisting the materialized projection (``compute``, ``rebuild``,
``rebuild_staged``). See ``app/services/stats/__init__.py`` for the canonical
engine surface.
"""

from __future__ import annotations

# discipline: file-size version-flip persistence seam; staged in adjacent publisher module

import math
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.summer_league.metric_publish import publish_metric_model
from app.services.summer_league.metric_publish import (
    METRIC_CALCULATION_VERSION,
    METRIC_REGISTRY_VERSION,
    next_metric_version,
    publish_metric_version,
)
from app.services.event_desk.timeutils import to_eastern_date

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

# Back-compat re-exports: the pure engine now lives in ``app.services.stats``
# (Phase 2, #722). These names keep every existing caller of this module working
# unchanged -- see ``app/services/stats/__init__.py`` for the canonical surface.
from app.services.stats.formulas import (  # noqa: F401
    ast_pct_line,
    compute_drtg,
    compute_metrics,
    compute_ortg,
    compute_uper,
    game_advanced_line,
    game_score,
    game_score_from_row,
    game_score_line,
    ftr_line,
    tov_pct_line,
    vorp82,
    vorp_total,
)
from app.services.stats.inputs import (  # noqa: F401
    BOX_INT_FIELDS as _BOX_INT_FIELDS,
    PlayerSeason,
    PoolContext as LeagueContext,
    StatInputs as Box,
    _d,
)
from app.services.stats.registry import (
    RollupClass,
    require_rollup_class,
    rollup_class_matches,
)
from app.services.summer_league import scoped_metrics
from app.services.summer_league.metric_trend_projection import materialize_trend_bands

ComputeResult = scoped_metrics.ComputeResult
MetricFit = scoped_metrics.MetricFit

# Qualification / gating thresholds.
QUALIFY_MIN_MINUTES = 40.0  # minutes for a season to feed fits / leaders
MIN_COMPLETE_TEAM_MP = 150.0  # sane regulation team minutes (~200 for 40-min)
# The pool-level pace floor now lives with the engine (`compute_metrics` is the
# only reader): app.services.stats.formulas.MIN_POOL_PACE.
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

# Registry-declared pool-recalibrated metrics guard reusable scoped fits.
_POOLED_FIT_METRICS = ("uper", "bpm", "ws82", "vorp82")
require_rollup_class(
    "summer_league.metrics pooled-fit projection",
    RollupClass.POOL_RECALIBRATED,
    *_POOLED_FIT_METRICS,
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

# NBA's player Advanced box endpoint supplies these player/team-box rates even
# while the corresponding team Advanced rows are incomplete. Keep the source
# names separate from the materialized season names: NBA calls total rebound
# percentage ``REB_PCT`` while the app's metric schema uses ``trb_pct``.
_SOURCE_RATE_COLUMNS: dict[str, str] = {
    "usg_pct": "usg_pct",
    "ast_pct": "ast_pct",
    "trb_pct": "reb_pct",
}


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
            ps.metrics["vorp"] = round(vorp_total(bpm, ps.box.mp), 2)
            # VORP/82: the same rate projected to a full 82-game season
            # (``pct_min`` is the share of available lineup-minutes). This is the
            # prior bare-"VORP" value, now labelled as the per-season pace.
            ps.metrics["vorp82"] = round(vorp82(bpm, ps.pct_min), 2)


@dataclass(frozen=True)
class RebuildOptions:
    """Persistence options shared by published and staged metric rebuilds."""

    model_version: Optional[str] = None
    competition_ids: Optional[Sequence[int]] = None
    publish: bool = True
    effective_day: Optional[date] = None
    through_day: Optional[date] = None


@dataclass(frozen=True)
class SeasonProjectionContext:
    """Version and eligibility metadata carried into one season projection row."""

    model_version: str
    publication_version: int
    as_of: Optional[datetime]
    effective_day: Optional[date]
    adv_eligible: bool


async def _source_as_of(
    db: AsyncSession,
    competition_ids: Optional[Collection[int]] = None,
    through_day: Optional[date] = None,
) -> Optional[datetime]:
    """Return the latest source update represented by a full or scoped build."""
    scope = frozenset(competition_ids) if competition_ids is not None else None
    statements = [
        select(func.max(SummerLeagueGame.updated_at)),
        select(func.max(SummerLeagueTeamGameLog.updated_at)),
        select(func.max(SummerLeaguePlayerGameLog.updated_at)),
        select(func.max(SummerLeagueShotEvent.updated_at)),
        select(func.max(SummerLeaguePlayByPlayEvent.updated_at)),
    ]
    models = (
        SummerLeagueGame,
        SummerLeagueTeamGameLog,
        SummerLeaguePlayerGameLog,
        SummerLeagueShotEvent,
        SummerLeaguePlayByPlayEvent,
    )
    game_date: Any = getattr(SummerLeagueGame, "game_date")
    game_id: Any = getattr(SummerLeagueGame, "id")
    team_game_id: Any = getattr(SummerLeagueTeamGameLog, "game_id")
    player_game_id: Any = getattr(SummerLeaguePlayerGameLog, "game_id")
    shot_game_id: Any = getattr(SummerLeagueShotEvent, "game_id")
    pbp_game_id: Any = getattr(SummerLeaguePlayByPlayEvent, "game_id")
    if scope is not None:
        statements = [
            statement.where(model.competition_id.in_(scope))  # type: ignore[attr-defined]
            for statement, model in zip(statements, models)
        ]
    if through_day is not None:
        # Every non-game source table carries competition_id but not game_date;
        # constrain it through its normalized game relation so ``as_of`` remains
        # the true watermark of exactly the rows included in the through-day fit.
        statements = [
            statements[0].where(game_date <= through_day),
            statements[1]
            .join(SummerLeagueGame, game_id == team_game_id)
            .where(game_date <= through_day),
            statements[2]
            .join(SummerLeagueGame, game_id == player_game_id)
            .where(game_date <= through_day),
            statements[3]
            .join(SummerLeagueGame, game_id == shot_game_id)
            .where(game_date <= through_day),
            statements[4]
            .join(SummerLeagueGame, game_id == pbp_game_id)
            .where(game_date <= through_day),
        ]
    timestamps = [await db.scalar(statement) for statement in statements]
    return max((stamp for stamp in timestamps if stamp is not None), default=None)


async def set_rebuild_idle_timeout(db: AsyncSession) -> None:
    """Disable the role idle timeout for the active metrics transaction."""
    await db.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))


async def set_repeatable_read_snapshot(db: AsyncSession) -> None:
    """Configure the unlocked metric build's transaction safeguards.

    The rebuild can spend several minutes fitting metrics in Python between SQL
    statements. The role-level ``idle_in_transaction_session_timeout`` from
    #576 is valuable for leaked sessions, but would terminate this legitimate
    ``REPEATABLE READ`` transaction during that compute gap. ``SET LOCAL``
    disables the reaper only until this transaction ends, preserving the role
    default for every other application session and pooled connection.
    """
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    await set_rebuild_idle_timeout(db)


async def _load_shot_diet(
    db: AsyncSession,
    competition_ids: Optional[Collection[int]] = None,
    through_day: Optional[date] = None,
) -> dict[tuple[int, int], dict[str, int]]:
    """Query shot-zone FGA counts aggregated per (player_id, competition_id).

    Excludes backcourt shots and rows with NULL player_id (unresolved).
    Returns a mapping of ``(player_id, competition_id)`` → zone → FGA count.
    """
    game_date: Any = getattr(SummerLeagueGame, "game_date")
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
    if competition_ids is not None:
        stmt = stmt.where(
            SummerLeagueShotEvent.competition_id.in_(competition_ids)  # type: ignore[attr-defined]
        )
    stmt = _through_day_filter(stmt, game_date, through_day)
    rows = (await db.execute(stmt)).all()
    out: dict[tuple[int, int], dict[str, int]] = defaultdict(dict)
    for player_id, competition_id, zone, fga in rows:
        out[(int(player_id), int(competition_id))][str(zone)] = int(fga)
    return out


async def _load_assisted_fg(
    db: AsyncSession,
    competition_ids: Optional[Collection[int]] = None,
    through_day: Optional[date] = None,
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
    game_date: Any = getattr(SummerLeagueGame, "game_date")
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
    if competition_ids is not None:
        stmt = stmt.where(
            SummerLeaguePlayByPlayEvent.competition_id.in_(competition_ids)  # type: ignore[attr-defined]
        )
    stmt = _through_day_filter(stmt, game_date, through_day)
    rows = (await db.execute(stmt)).all()
    return {
        (int(player_id), int(competition_id)): (int(ast_fgm), int(unast_fgm))
        for player_id, competition_id, ast_fgm, unast_fgm in rows
    }


def _through_day_filter(
    statement: Any, game_date: Any, through_day: date | None
) -> Any:
    """Apply a historical game-date predicate when a cutoff is requested."""
    if through_day is None:
        return statement
    return statement.where(game_date <= through_day)


def _latest_competition_game_days(game_rows: Sequence[Any]) -> dict[int, date]:
    """Return each competition's latest included normalized game date."""
    latest: dict[int, date] = {}
    for game in game_rows:
        if game.game_date is not None:
            latest[game.competition_id] = max(
                game.game_date,
                latest.get(game.competition_id, game.game_date),
            )
    return latest


async def _load(
    db: AsyncSession,
    competition_ids: Optional[Collection[int]] = None,
    through_day: Optional[date] = None,
) -> tuple[Any, ...]:
    """Load the raw rows needed for a full build or selected competitions."""
    comp = SummerLeagueCompetition
    tgl = SummerLeagueTeamGameLog
    pgl = SummerLeaguePlayerGameLog
    scope = frozenset(competition_ids) if competition_ids is not None else None
    game_date: Any = getattr(SummerLeagueGame, "game_date")

    comp_id: Any = comp.id
    comps_stmt = select(comp)
    if scope is not None:
        comps_stmt = comps_stmt.where(comp_id.in_(scope))
    comps = {
        c.id: (c.year, c.venue_slug) for c in (await db.execute(comps_stmt)).scalars()
    }

    games_stmt = select(SummerLeagueGame).where(_season_game_status_clause())
    if scope is not None:
        games_stmt = games_stmt.where(
            SummerLeagueGame.competition_id.in_(scope)  # type: ignore[attr-defined]
        )
    games_stmt = _through_day_filter(games_stmt, game_date, through_day)
    game_rows = list((await db.execute(games_stmt)).scalars())
    games = {
        g.id: (g.home_team_entry_id, g.away_team_entry_id, g.home_score, g.away_score)
        for g in game_rows
    }
    competition_effective_days = _latest_competition_game_days(game_rows)
    team_stmt = (
        select(tgl)
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == tgl.game_id,  # type: ignore[arg-type]
        )
        .where(_season_game_status_clause())
    )
    if scope is not None:
        team_stmt = team_stmt.where(tgl.competition_id.in_(scope))  # type: ignore[attr-defined]
    team_stmt = _through_day_filter(team_stmt, game_date, through_day)
    team_rows = (await db.execute(team_stmt)).scalars().all()
    team_mp_stmt = (
        select(pgl.team_entry_id, func.sum(pgl.minutes_seconds))  # type: ignore[call-overload]
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == pgl.game_id,  # type: ignore[arg-type]
        )
        .where(_season_game_status_clause())
        .group_by(pgl.team_entry_id)
    )
    if scope is not None:
        team_mp_stmt = team_mp_stmt.where(
            pgl.competition_id.in_(scope)  # type: ignore[attr-defined]
        )
    team_mp_stmt = _through_day_filter(team_mp_stmt, game_date, through_day)
    team_mp = {
        tid: (sec or 0) / 60.0 for tid, sec in (await db.execute(team_mp_stmt)).all()
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
    player_stmt = (
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
    if scope is not None:
        player_stmt = player_stmt.where(pgl.competition_id.in_(scope))  # type: ignore[attr-defined]
    player_stmt = _through_day_filter(player_stmt, game_date, through_day)
    player_rows = (await db.execute(player_stmt)).all()
    return comps, games, team_rows, team_mp, player_rows, competition_effective_days


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


async def _load_active_fit(db: AsyncSession) -> Optional[MetricFit]:
    """Load the active pooled fit, if a full rebuild has published one."""
    stmt = (
        select(SummerLeagueMetricModel)  # type: ignore[call-overload]
        .where(SummerLeagueMetricModel.is_active.is_(True))  # type: ignore[attr-defined]
        .order_by(SummerLeagueMetricModel.id.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    model = (await db.execute(stmt)).scalar_one_or_none()
    if model is None:
        return None
    return MetricFit(
        pyth_exponent=model.pyth_exponent,
        pyth_n=model.pyth_n_teams,
        ws_ppw_coeff=model.ws_ppw_coeff,
        bpm_coef=dict(model.bpm_coefficients) if model.bpm_coefficients else None,
        bpm_intercept=model.bpm_intercept,
        bpm_r2=model.bpm_r2,
        bpm_n_fit=model.bpm_n_fit,
        model_version=model.model_version,
    )


def _can_reuse_pooled_fit() -> bool:
    """Return whether the registry still supports the scoped-fit contract."""
    return all(
        rollup_class_matches(metric, RollupClass.POOL_RECALIBRATED)
        for metric in _POOLED_FIT_METRICS
    )


async def _load_compute_inputs(
    db: AsyncSession,
    *,
    competition_ids: Optional[Sequence[int]],
    pooled_fit: Optional[MetricFit],
    through_day: Optional[date],
) -> tuple[Any, ...]:
    """Load source rows for one compute call, preserving legacy test seams."""
    scope = frozenset(competition_ids) if competition_ids is not None else None
    load_scope = scope if pooled_fit is not None else None
    if load_scope is None and through_day is None and competition_ids is None:
        as_of = await _source_as_of(db)
        (
            comps,
            games,
            team_rows,
            team_mp,
            player_rows,
            competition_effective_days,
        ) = await _load(db)
        shot_diet = await _load_shot_diet(db)
        assisted_fg = await _load_assisted_fg(db)
    elif load_scope is None:
        # A first scoped call without an active fit bootstraps the fit from the full
        # through-day source pool, then narrows projections after assembly.
        # This branch fits against the full through-day pool, so its watermark
        # must cover that same pool even though only the requested projections
        # are retained below.
        as_of = await _source_as_of(db, through_day=through_day)
        (
            comps,
            games,
            team_rows,
            team_mp,
            player_rows,
            competition_effective_days,
        ) = await _load(db, through_day=through_day)
        shot_diet = await _load_shot_diet(db, through_day=through_day)
        assisted_fg = await _load_assisted_fg(db, through_day=through_day)
    else:
        as_of = await _source_as_of(db, load_scope, through_day=through_day)
        (
            comps,
            games,
            team_rows,
            team_mp,
            player_rows,
            competition_effective_days,
        ) = await _load(db, load_scope, through_day=through_day)
        shot_diet = await _load_shot_diet(db, load_scope, through_day=through_day)
        assisted_fg = await _load_assisted_fg(db, load_scope, through_day=through_day)
    return (
        as_of,
        comps,
        games,
        team_rows,
        team_mp,
        player_rows,
        competition_effective_days,
        shot_diet,
        assisted_fg,
    )


async def compute(
    db: AsyncSession,
    *,
    competition_ids: Optional[Sequence[int]] = None,
    fit: Optional[MetricFit] = None,
    through_day: Optional[date] = None,
) -> ComputeResult:
    """Load and project metrics, optionally through one historical event day.

    ``through_day`` constrains every source query through the normalized game date;
    all formulas and projection assembly remain the shared Summer League engine.
    """
    scope = frozenset(competition_ids) if competition_ids is not None else None
    pooled_fit = fit
    # A retained historical close must be fit only from information available
    # through that event day. The active fit may include later games or later
    # seasons, so it is reusable for live scoped ticks only.
    if (
        scope is not None
        and through_day is None
        and pooled_fit is None
        and _can_reuse_pooled_fit()
    ):
        pooled_fit = await _load_active_fit(db)

    (
        as_of,
        comps,
        games,
        team_rows,
        team_mp,
        player_rows,
        competition_effective_days,
        shot_diet,
        assisted_fg,
    ) = await _load_compute_inputs(
        db,
        competition_ids=competition_ids,
        pooled_fit=pooled_fit,
        through_day=through_day,
    )
    load_scope = scope if pooled_fit is not None else None
    team_box, opp_box, team_comp, contexts, records = _build(
        comps, games, team_rows, team_mp
    )
    seasons = scoped_metrics.assemble_seasons(
        comps, team_box, opp_box, player_rows, _SOURCE_RATE_COLUMNS
    )
    projected_fit = scoped_metrics.project_metrics(
        seasons, contexts, records, team_comp, pooled_fit
    )
    competition_trend_bands, season_trend_bands = materialize_trend_bands(
        seasons,
        include_season_scope=load_scope is None,
    )

    if scope is not None and load_scope is None:
        contexts = {cid: ctx for cid, ctx in contexts.items() if cid in scope}
        seasons = [ps for ps in seasons if ps.competition_id in scope]
        shot_diet = {key: value for key, value in shot_diet.items() if key[1] in scope}
        assisted_fg = {
            key: value for key, value in assisted_fg.items() if key[1] in scope
        }

    return ComputeResult(
        fit=projected_fit,
        contexts=contexts,
        seasons=seasons,
        shot_diet=shot_diet,
        assisted_fg=assisted_fg,
        competition_trend_bands=competition_trend_bands,
        season_trend_bands=season_trend_bands,
        competition_effective_days=competition_effective_days,
        as_of=as_of,
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _season_columns(
    ps: PlayerSeason,
    projection: SeasonProjectionContext,
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
        "adv_eligible": projection.adv_eligible,
        "model_version": projection.model_version,
        "version": projection.publication_version,
        "is_current": False,
        "registry_version": METRIC_REGISTRY_VERSION,
        "calculation_version": METRIC_CALCULATION_VERSION,
        "as_of": projection.as_of,
        "effective_day": projection.effective_day,
        "trend_competition_bands": None,
        "trend_season_bands": None,
        "published_at": None,
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


async def _rebuild_with_options(
    db: AsyncSession, options: RebuildOptions
) -> dict[str, Any]:
    """Recompute materialized SL metric tables -- full rebuild, or scoped by competition.

    Two modes, selected by ``competition_ids``:

    * **Unscoped** (default, ``competition_ids=None``) -- a full build. New
      rows are appended as an inactive candidate version; the old current
      version remains readable until :func:`publish_metric_version` flips it.
    * **Scoped** (``competition_ids`` a sequence of competition ids) -- only
      candidate rows for those competition ids are written. When ``publish``
      is true (the Desk tick default), those scopes are flipped current before
      this function returns. When false, the caller can include the flip in a
      separate short publication transaction.

    A full :func:`compute` fits over the entire raw dataset. A scoped
    :func:`compute` reuses the active full-pool fit and loads only its requested
    competition rows; the registry's ``pool_recalibrated`` declarations guard
    that reuse. If no active fit exists, the scoped call bootstraps one full fit
    before narrowing the returned projections.

    Returns a small summary dict (counts of what was actually written). The
    caller controls the transaction. :func:`rebuild_staged` is used by the full
    ingestion pipeline so materialization can finish before publication.
    """
    model_version = options.model_version
    competition_ids = options.competition_ids
    publish = options.publish
    # Explicit historical callers provide one cutoff day. Normal rebuilds stamp
    # each competition from its latest included game, with the Eastern process
    # day only as a fallback for a malformed pool that has no dated game.
    fallback_effective_day = to_eastern_date(datetime.now(timezone.utc))
    if competition_ids is not None and not competition_ids:
        return {
            "seasons": 0,
            "contexts": 0,
            "adv_pools": 0,
            "version": 0,
            "model_version": model_version or "",
            "as_of": None,
            "published": publish,
        }

    # Both full and scoped rebuilds fit the entire source pool in Python. Keep
    # the role-level leak guard from terminating a legitimate transaction when
    # this path is called directly by the Desk tick without the full-ingestion
    # snapshot helper.
    await set_rebuild_idle_timeout(db)
    result = await compute(
        db,
        competition_ids=competition_ids,
        through_day=options.through_day,
    )
    competition_effective_days = getattr(result, "competition_effective_days", {})
    adv_cids = {cid for cid, ctx in result.contexts.items() if ctx.adv_eligible}
    scope = frozenset(competition_ids) if competition_ids is not None else None
    publication_version = await next_metric_version(db)
    generated_model_version = model_version or datetime.now(timezone.utc).strftime(
        "%Y%m%d%H%M%S"
    )

    if scope is None:
        # Stage the fit inactive. The publication transaction activates it with
        # both projection tables, so a failed materialization leaves the old fit
        # active and auditable.
        await publish_metric_model(
            db,
            version=generated_model_version,
            result=result,
            activate=False,
        )
        contexts_to_write = list(result.contexts.values())
        seasons_to_write = result.seasons
    else:
        # Scoped ticks reuse the active global fit and only stage the selected
        # competition scopes.
        result_fit = getattr(result, "fit", None)
        fit_model_version = getattr(result_fit, "model_version", None)
        generated_model_version = (
            model_version
            or fit_model_version
            or await _active_or_fresh_model_version(db)
        )
        if result_fit is not None and fit_model_version is None:
            # A first-ever scoped rebuild bootstrapped a full fit because no
            # active model existed. Retain it now so the next tick can take the
            # genuinely scoped path instead of repeating the bootstrap.
            await publish_metric_model(
                db,
                version=generated_model_version,
                result=result,
                activate=publish,
            )
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
                version=publication_version,
                is_current=False,
                registry_version=METRIC_REGISTRY_VERSION,
                calculation_version=METRIC_CALCULATION_VERSION,
                as_of=result.as_of,
                published_at=None,
                effective_day=(
                    options.effective_day
                    or competition_effective_days.get(ctx.competition_id)
                    or fallback_effective_day
                ),
            )
        )

    n_seasons = 0
    for ps in seasons_to_write:
        zone_fga = result.shot_diet.get((ps.player_id, ps.competition_id), {})
        pbp_counts = result.assisted_fg.get((ps.player_id, ps.competition_id))
        cols = _season_columns(
            ps,
            SeasonProjectionContext(
                model_version=generated_model_version,
                publication_version=publication_version,
                as_of=result.as_of,
                effective_day=(
                    options.effective_day
                    or competition_effective_days.get(ps.competition_id)
                    or fallback_effective_day
                ),
                adv_eligible=ps.competition_id in adv_cids,
            ),
            zone_fga,
            pbp_counts,
        )
        cols["trend_competition_bands"] = result.competition_trend_bands.get(
            ps.competition_id
        )
        cols["trend_season_bands"] = result.season_trend_bands.get(ps.year)
        db.add(SummerLeaguePlayerSeason(**cols))
        n_seasons += 1

    summary: dict[str, Any] = {
        "seasons": n_seasons,
        "contexts": len(contexts_to_write),
        "adv_pools": len(adv_cids if scope is None else adv_cids & scope),
        "version": publication_version,
        "model_version": generated_model_version,
        "published": publish,
    }
    if publish:
        await publish_metric_version(
            db,
            version=publication_version,
            competition_ids=scope,
            model_version=generated_model_version if scope is None else None,
            as_of=result.as_of,
            effective_day=options.effective_day,
        )
    # Keep the source watermark alongside the row counts so the gate can carry the
    # exact input currency into the final publication transaction.
    summary["as_of"] = result.as_of
    # A full build contains independent event calendars. Omitting a single
    # run-wide stamp preserves each candidate row's competition-derived day at
    # publication; explicit historical/scoped callers still carry one exact day.
    summary["effective_day"] = options.effective_day
    return summary


async def rebuild(
    db: AsyncSession,
    *,
    model_version: Optional[str] = None,
    competition_ids: Optional[Sequence[int]] = None,
    effective_day: Optional[date] = None,
    through_day: Optional[date] = None,
) -> dict[str, Any]:
    """Build and publish a full or scoped metric projection."""
    return await _rebuild_with_options(
        db,
        RebuildOptions(
            model_version=model_version,
            competition_ids=competition_ids,
            effective_day=effective_day,
            through_day=through_day,
        ),
    )


async def rebuild_staged(
    db: AsyncSession,
    *,
    model_version: Optional[str] = None,
    competition_ids: Optional[Sequence[int]] = None,
    effective_day: Optional[date] = None,
    through_day: Optional[date] = None,
) -> dict[str, Any]:
    """Build an inactive metric projection for a later atomic publication."""
    return await _rebuild_with_options(
        db,
        RebuildOptions(
            model_version=model_version,
            competition_ids=competition_ids,
            publish=False,
            effective_day=effective_day,
            through_day=through_day,
        ),
    )

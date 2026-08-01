"""Read-side service for materialized Summer League advanced metrics.

Reads the pre-computed :class:`SummerLeaguePlayerSeason` rows (one per player +
competition) for display. The composite metrics are *league-relative*: PER is
standardized to 15 within each pool, BPM is re-centered to 0.0 per pool, and Win
Shares come from each pool's own Pythagorean fit. That recalibration is only
meaningful **inside a single competition**, which drives how this service shapes
data:

* **Per-competition rows** surface the full basket exactly as materialized.
* **Career rollups** only sum the *additive* shares (Win Shares, VORP) across
  adv-eligible pools. Rate/centered composites (PER, BPM, ratings) are never
  rolled into a cross-pool headline; PER/BPM are exposed only as a
  minute-weighted average, explicitly labeled as such.

``adv_eligible`` gates the composites: a pool earns it only when enough games
have complete boxes and enough players qualify. When ``False`` the row's
composite columns are ``None``. This service therefore returns **only
adv-eligible competitions**, so an advanced view never renders a row of all
em-dashes — non-eligible pools still appear in the raw box-score table elsewhere.
"""

from __future__ import annotations

# discipline: file-size current-projection read predicate; no new metrics service surface

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.stats.capabilities import is_computable
from app.services.stats.formulas import (
    astd_pct_line,
    efg_pct_line,
    fg3ar_line,
    ftr_line,
    win_shares_per_40,
    tov_pct_line,
    ts_pct_line,
)
from app.services.stats.registry import RollupClass, rollup_class_matches
from app.services.summer_league.capabilities import row_provides, rows_provide

# Minimum total minutes in a competition before its rate composites are
# trustworthy enough to surface. Small-sample pools blow PER/BPM up well past
# any real scale (a sub-game appearance can read PER 70+), so we hide them.
# Matches ``metrics.QUALIFY_MIN_MINUTES`` — the same floor the computation uses
# to decide which players feed the league fits.
DISPLAY_MIN_MINUTES = 40.0

# Human-readable venue labels keyed by ``venue_slug`` (mirrors the stats service).
VENUE_LABELS: dict[str, str] = {
    "las_vegas": "Las Vegas",
    "salt_lake_city": "Salt Lake City",
    "california_classic": "California Classic",
    "orlando": "Orlando",
}

# Compact venue tags used to disambiguate same-year competition rows.
VENUE_ABBR: dict[str, str] = {
    "las_vegas": "LV",
    "salt_lake_city": "SLC",
    "california_classic": "CC",
    "orlando": "ORL",
}

# Display order within a year (marquee Las Vegas first).
_VENUE_ORDER: dict[str, int] = {
    "las_vegas": 0,
    "salt_lake_city": 1,
    "california_classic": 2,
    "orlando": 3,
}


@dataclass
class PlayerMetricSeason:
    """SL-calibrated advanced metrics for one (player, competition).

    Every field is populated because the service only returns adv-eligible
    competitions; individual columns may still be ``None`` for the rare pool that
    lacked an input (the template renders those as em-dashes).
    """

    year: int
    venue_slug: str
    venue_label: str
    venue_abbr: str
    gp: int
    minutes: float
    # Raw box totals, kept so career TS% / 3PAr / FTr / TOV% can pool volume
    # (these rates are weighted by shooting/play volume, not minutes).
    pts: int
    fga: int
    fg3a: int
    fta: int
    tov: int
    # Shooting / efficiency.
    ts_pct: Optional[float]
    efg_pct: Optional[float]
    gmsc: Optional[float]
    # Attempt rates (0-1 fractions, unlike the 0-100 percentage columns).
    fg3ar: Optional[float]
    ftr: Optional[float]
    # PBP assisted-FG counts (None outside the PBP era) and the derived
    # assisted share of made FGs (0-100).
    ast_fgm: Optional[int]
    unast_fgm: Optional[int]
    astd_pct: Optional[float]
    # Rate.
    usg_pct: Optional[float]
    ast_pct: Optional[float]
    tov_pct: Optional[float]
    orb_pct: Optional[float]
    drb_pct: Optional[float]
    trb_pct: Optional[float]
    stl_pct: Optional[float]
    blk_pct: Optional[float]
    # Composites (league-relative, valid only within this pool).
    per: Optional[float]
    ortg: Optional[float]
    drtg: Optional[float]
    net_rtg: Optional[float]
    obpm: Optional[float]
    dbpm: Optional[float]
    # Value metrics come in two flavours: a cumulative stat (accrued over the
    # games played) and an 82-game projection (the per-season pace).
    ws: Optional[float]  # cumulative
    ows: Optional[float]
    dws: Optional[float]
    ws40: Optional[float]
    ws82: Optional[float]  # projected to 82 games
    bpm: Optional[float]
    vorp: Optional[float]  # cumulative
    vorp82: Optional[float]  # projected to 82 games


@dataclass
class PlayerMetricCareer:
    """Career rollup across a player's adv-eligible competitions.

    Only the additive shares (Win Shares, VORP) are summed. ``per_avg`` and
    ``bpm_avg`` are minute-weighted means kept as soft "career average" context,
    never as a recalibrated headline. ``ts_avg`` instead pools the raw shot
    totals (TS% is weighted by shooting possessions, not minutes).
    """

    adv_pools: int
    gp: int
    minutes: float
    # Cumulative shares are summed across competitions.
    ws: Optional[float]
    vorp: Optional[float]
    ws40: Optional[float]
    # Rates/projections are minute-weighted averages, never summed.
    per_avg: Optional[float]
    ts_avg: Optional[float]
    bpm_avg: Optional[float]
    ws82_avg: Optional[float]
    vorp82_avg: Optional[float]
    # Volume-pooled rates recomputed from summed box totals (exact, not
    # averaged). AST% and the rebound/steal/block rates have no career form
    # here — they need per-pool team/opponent context.
    ftr: Optional[float] = None
    tov_pct: Optional[float] = None
    fg3ar: Optional[float] = None
    # Assisted share of made FGs pools exactly from summed PBP counts.
    astd_pct: Optional[float] = None
    # Win Shares components sum like WS; BPM splits minute-weight like BPM.
    ows: Optional[float] = None
    dws: Optional[float] = None
    obpm_avg: Optional[float] = None
    dbpm_avg: Optional[float] = None


@dataclass
class PlayerMetricsProfile:
    """A player's advanced-metric profile for the player-detail SL section."""

    seasons: list[PlayerMetricSeason]
    career: PlayerMetricCareer


def _weighted_mean(
    pairs: list[tuple[Optional[float], float]],
) -> Optional[float]:
    """Minute-weighted mean of ``(value, minutes)`` pairs, ``None`` when empty.

    Pairs whose value is ``None`` are skipped (they contribute no weight), so a
    pool missing one composite does not drag the average toward zero.
    """
    num = 0.0
    den = 0.0
    for value, minutes in pairs:
        if value is None or minutes <= 0:
            continue
        num += value * minutes
        den += minutes
    return num / den if den else None


def _pooled_ts(seasons: list[PlayerMetricSeason]) -> Optional[float]:
    """Career True Shooting % pooled from raw shot totals across competitions.

    Delegates the formula to :func:`app.services.stats.formulas.ts_pct_line`.
    Aggregating it correctly means summing the underlying possessions first,
    not minute-weighting each pool's percentage — a player with uneven shot
    volume per minute would otherwise read a misstated career mark. Returned
    on the same 0-100 scale as the stored per-pool values; ``None`` when there
    are no true-shooting attempts to divide by.
    """
    pts = sum(s.pts for s in seasons)
    fga = sum(s.fga for s in seasons)
    fta = sum(s.fta for s in seasons)
    return ts_pct_line(pts=pts, fga=fga, fta=fta)


# T8b (#729): the advanced-metrics wiring's per-field rollup handling below is
# checked against ``app.services.stats.registry``'s ``rollup_class`` instead of
# being re-derived only in the comments/docstring above. ``ws``/``vorp`` are the
# registry's only ``additive_share`` entries (their ``ows``/``dws`` components,
# and ``per``/``obpm``/``dbpm``, aren't registered separately -- T7 scoped the
# registry to the metrics T4-T6 actually consolidated).
_CAREER_SUMMED_KEYS: tuple[str, ...] = ("ws", "vorp")
assert all(
    rollup_class_matches(k, RollupClass.ADDITIVE_SHARE) for k in _CAREER_SUMMED_KEYS
), "_career's summed keys must stay registry-declared additive_share"

# ``ftr``/``tov_pct``/``fg3ar`` are recomputed from this player's own summed box
# volume (fga/fta/fg3a/tov -- all season-level columns), matching the registry's
# ``recombinable`` contract exactly, unlike the minute-weighted approximations
# below.
_CAREER_RECOMBINED_KEYS: tuple[str, ...] = ("ftr", "tov_pct", "fg3ar")
assert all(
    rollup_class_matches(k, RollupClass.RECOMBINABLE) for k in _CAREER_RECOMBINED_KEYS
), "_career's box-recombined keys must stay registry-declared recombinable"

# ``ws82_avg``/``vorp82_avg`` are declared ``pool_recalibrated`` in the registry
# ("must be recomputed against the pool context ... never averaged") but this
# site minute-weight-averages them anyway, same as ``per_avg``/``bpm_avg``/
# ``obpm_avg``/``dbpm_avg``. That is a deliberate, already-labeled exception --
# see :class:`PlayerMetricCareer`'s docstring ("soft 'career average' context,
# never a recalibrated headline") -- not a silent re-derivation of the taxonomy,
# so it is flagged here rather than resolved (T8b / #729 scope discipline; see
# the sibling conflict in :func:`_blend_leader_values`).
assert rollup_class_matches("ws82", RollupClass.POOL_RECALIBRATED)
assert rollup_class_matches("vorp82", RollupClass.POOL_RECALIBRATED)


def _career(seasons: list[PlayerMetricSeason]) -> PlayerMetricCareer:
    """Roll adv-eligible seasons into a career line per the grain rules."""
    minutes = sum(s.minutes for s in seasons)
    ws_vals = [s.ws for s in seasons if s.ws is not None]
    vorp_vals = [s.vorp for s in seasons if s.vorp is not None]
    ws_total = sum(ws_vals) if ws_vals else None
    # Recompute WS/40 from the summed shares so it stays internally consistent.
    ws40 = win_shares_per_40(ws_total, minutes) if ws_total is not None else None
    # FTr / 3PAr / TOV% pool exactly from summed box volume (like career TS%).
    fga = sum(s.fga for s in seasons)
    fg3a = sum(s.fg3a for s in seasons)
    fta = sum(s.fta for s in seasons)
    tov = sum(s.tov for s in seasons)
    ows_vals = [s.ows for s in seasons if s.ows is not None]
    dws_vals = [s.dws for s in seasons if s.dws is not None]
    return PlayerMetricCareer(
        adv_pools=len(seasons),
        gp=sum(s.gp for s in seasons),
        minutes=minutes,
        ws=round(ws_total, 1) if ws_total is not None else None,
        vorp=round(sum(vorp_vals), 1) if vorp_vals else None,
        ws40=round(ws40, 2) if ws40 is not None else None,
        per_avg=_round1(_weighted_mean([(s.per, s.minutes) for s in seasons])),
        ts_avg=_pooled_ts(seasons),
        bpm_avg=_round1(_weighted_mean([(s.bpm, s.minutes) for s in seasons])),
        ws82_avg=_round1(_weighted_mean([(s.ws82, s.minutes) for s in seasons])),
        vorp82_avg=_round1(_weighted_mean([(s.vorp82, s.minutes) for s in seasons])),
        ftr=ftr_line(fga=fga, fta=fta),
        tov_pct=tov_pct_line(fga=fga, fta=fta, tov=tov),
        fg3ar=fg3ar_line(fg3a=fg3a, fga=fga),
        astd_pct=(
            _assisted_share(
                sum((s.ast_fgm or 0) for s in seasons),
                sum((s.unast_fgm or 0) for s in seasons),
            )
            if is_computable("astd_pct", rows_provide(seasons))
            else None
        ),
        ows=round(sum(ows_vals), 1) if ows_vals else None,
        dws=round(sum(dws_vals), 1) if dws_vals else None,
        obpm_avg=_round1(_weighted_mean([(s.obpm, s.minutes) for s in seasons])),
        dbpm_avg=_round1(_weighted_mean([(s.dbpm, s.minutes) for s in seasons])),
    )


def _round1(value: Optional[float]) -> Optional[float]:
    return round(value, 1) if value is not None else None


def _round3(value: Optional[float]) -> Optional[float]:
    return round(value, 3) if value is not None else None


def _assisted_share(
    ast_fgm: Optional[int], unast_fgm: Optional[int]
) -> Optional[float]:
    """Assisted share of made FGs (0-100) from PBP counts; None without PBP data."""
    return astd_pct_line(ast_fgm=ast_fgm, unast_fgm=unast_fgm)


def _to_season(row: SummerLeaguePlayerSeason) -> PlayerMetricSeason:
    """Map a materialized row to the display dataclass."""
    return PlayerMetricSeason(
        year=row.year,
        venue_slug=row.venue_slug,
        venue_label=VENUE_LABELS.get(row.venue_slug, row.venue_slug),
        venue_abbr=VENUE_ABBR.get(row.venue_slug, row.venue_slug),
        gp=row.gp,
        minutes=row.minutes,
        pts=row.pts,
        fga=row.fga,
        fg3a=row.fg3a,
        fta=row.fta,
        tov=row.tov,
        # Shooting / rate columns are already stored as percentages (e.g. 60.6),
        # not 0-1 fractions, so they only need rounding — not a ×100 rescale.
        ts_pct=_round1(row.ts_pct),
        efg_pct=_round1(row.efg_pct),
        gmsc=_round1(row.gmsc),
        fg3ar=_round3(row.fg3ar),
        ftr=_round3(row.ftr),
        ast_fgm=row.ast_fgm,
        unast_fgm=row.unast_fgm,
        astd_pct=(
            _assisted_share(row.ast_fgm, row.unast_fgm)
            if is_computable("astd_pct", row_provides(row))
            else None
        ),
        usg_pct=_round1(row.usg_pct),
        ast_pct=_round1(row.ast_pct),
        tov_pct=_round1(row.tov_pct),
        orb_pct=_round1(row.orb_pct),
        drb_pct=_round1(row.drb_pct),
        trb_pct=_round1(row.trb_pct),
        stl_pct=_round1(row.stl_pct),
        blk_pct=_round1(row.blk_pct),
        per=_round1(row.per),
        ortg=_round1(row.ortg),
        drtg=_round1(row.drtg),
        net_rtg=_round1(row.net_rtg),
        obpm=_round1(row.obpm),
        dbpm=_round1(row.dbpm),
        ws=_round1(row.ws),
        ows=_round1(row.ows),
        dws=_round1(row.dws),
        ws40=round(row.ws40, 2) if row.ws40 is not None else None,
        ws82=_round1(row.ws82),
        bpm=_round1(row.bpm),
        vorp=_round1(row.vorp),
        vorp82=_round1(row.vorp82),
    )


async def get_player_metric_seasons(
    db: AsyncSession,
    player_id: int,
) -> Optional[PlayerMetricsProfile]:
    """Fetch a player's SL advanced metrics across adv-eligible competitions.

    Args:
        db: Async database session.
        player_id: Canonical ``players_master`` id.

    Returns:
        A :class:`PlayerMetricsProfile` (newest competition first, marquee Las
        Vegas first within a year), or ``None`` when the player has no
        adv-eligible Summer League competition with at least
        :data:`DISPLAY_MIN_MINUTES` minutes, so the caller can omit the view.
    """
    stmt = select(SummerLeaguePlayerSeason).where(
        SummerLeaguePlayerSeason.player_id == player_id,  # type: ignore[arg-type]
        SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.adv_eligible.is_(True),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.minutes >= DISPLAY_MIN_MINUTES,  # type: ignore[arg-type]
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return None

    # Newest year first; marquee Las Vegas first within a year. The Python sort is
    # total and deterministic, so no DB-side ORDER BY is needed.
    rows.sort(key=lambda r: (-r.year, _VENUE_ORDER.get(r.venue_slug, 99)))
    seasons = [_to_season(r) for r in rows]
    return PlayerMetricsProfile(seasons=seasons, career=_career(seasons))


# --------------------------------------------------------------------------- #
# Per-competition leaderboard
# --------------------------------------------------------------------------- #
# The composites are recalibrated within each pool, so a leaderboard is only
# meaningful inside a single competition (year + venue). The value columns shown
# are the per-competition basket — cumulative WS/VORP rank the same as their /82
# projections within one pool, so the cumulative form is enough here.
ADV_LEADER_COLUMNS: tuple[str, ...] = (
    "min",
    "per",
    "ts_pct",
    "efg_pct",
    "fg3ar",
    "ftr",
    "usg_pct",
    "ortg",
    "drtg",
    "orb_pct",
    "drb_pct",
    "trb_pct",
    "ast_pct",
    "stl_pct",
    "blk_pct",
    "tov_pct",
    "obpm",
    "dbpm",
    "bpm",
    "ows",
    "dws",
    "ws",
    "ws40",
    "vorp",
    "gmsc",
)


@dataclass
class CompetitionLeaderRow:
    """One player's row on a per-competition advanced leaderboard."""

    slug: Optional[str]
    name: str
    gp: int
    values: dict[str, Optional[float]]


@dataclass
class CompetitionLeaders:
    """An adv-eligible competition's leaderboard plus the picker's options.

    ``year``/``venue_slug`` are the *resolved* competition (may differ from the
    request when it defaulted). ``competitions`` lists every adv-eligible
    ``(year, venue_slug)`` newest-first for the selector. Empty ``rows`` with a
    ``None`` competition means no adv-eligible pool exists at all.

    ``calibrated`` is ``False`` when the rows come from a pool that has not yet
    earned ``adv_eligible`` (e.g. a competition mid-event): the box-derived
    rates (MIN, GmSc, TS%, eFG%, 3PAr, FTr) are populated but the
    pool-calibrated composites are ``None``.

    ``min_games``/``min_minutes`` are the qualification gate actually enforced
    on ``rows`` — the first ladder rung that matched anyone, with the
    :data:`DISPLAY_MIN_MINUTES` floor folded in where it applies.
    """

    year: Optional[int]
    venue_slug: Optional[str]
    venue_label: Optional[str]
    competitions: list[tuple[int, str]]
    rows: list[CompetitionLeaderRow]
    calibrated: bool = True
    min_games: int = 0
    min_minutes: int = 0


# Default qualification "ladder": a single fully-open rung. Callers pass a
# multi-rung ladder to relax thresholds until the board populates.
OPEN_GATES: tuple[tuple[int, int], ...] = ((0, 0),)


def _first_populated_rung(
    gates: tuple[tuple[int, int], ...],
    floor: float,
    seasons_by_key: dict[int, tuple[int, float]],
    min_rows: int = 1,
) -> tuple[set[int], int, int]:
    """Walk ``gates`` rung by rung over pre-fetched qualification totals.

    Args:
        gates: ``(min_games, min_minutes)`` rungs, strictest first.
        floor: Minutes floor folded into every rung (0 when none applies).
        seasons_by_key: ``{key: (gp, minutes)}`` for each candidate row.
        min_rows: A rung only "populates" the board once it matches at least
            this many players; below that the walk keeps relaxing.

    Returns:
        ``(qualifying keys, applied min_games, applied effective min_minutes)``
        for the first rung that matches ``min_rows`` players; the last rung's
        result when none does. Rungs that collapse to an already-tried
        effective gate are skipped.
    """
    qualified: set[int] = set()
    applied = (gates[0][0], int(max(gates[0][1], floor)))
    tried: set[tuple[int, int]] = set()
    for g, m in gates:
        effective = (g, int(max(m, floor)))
        if effective in tried:
            continue
        tried.add(effective)
        applied = effective
        qualified = {
            k
            for k, (gp, minutes) in seasons_by_key.items()
            if gp >= effective[0] and minutes >= effective[1]
        }
        if len(qualified) >= min_rows:
            break
    return qualified, applied[0], applied[1]


def _leader_values(s: SummerLeaguePlayerSeason) -> dict[str, Optional[float]]:
    """Shape one materialized row into display values keyed by leader column."""
    return {
        "min": _round1(s.minutes),
        "per": _round1(s.per),
        "ts_pct": _round1(s.ts_pct),
        "efg_pct": _round1(s.efg_pct),
        # Attempt rates are stored as 0-1 fractions at 3 dp (unlike the 0-100
        # percentage columns), matching BBRef's 3PAr / FTr presentation.
        "fg3ar": _round3(s.fg3ar),
        "ftr": _round3(s.ftr),
        "usg_pct": _round1(s.usg_pct),
        "ortg": _round1(s.ortg),
        "drtg": _round1(s.drtg),
        "orb_pct": _round1(s.orb_pct),
        "drb_pct": _round1(s.drb_pct),
        "trb_pct": _round1(s.trb_pct),
        "ast_pct": _round1(s.ast_pct),
        "stl_pct": _round1(s.stl_pct),
        "blk_pct": _round1(s.blk_pct),
        "tov_pct": _round1(s.tov_pct),
        "obpm": _round1(s.obpm),
        "dbpm": _round1(s.dbpm),
        "bpm": _round1(s.bpm),
        "ows": _round1(s.ows),
        "dws": _round1(s.dws),
        "ws": _round1(s.ws),
        # WS/40 lives in the hundredths; keep 2 decimals so sorting and display
        # don't collapse to 0.0 (matches _to_season / _career).
        "ws40": round(s.ws40, 2) if s.ws40 is not None else None,
        "vorp": _round1(s.vorp),
        "gmsc": _round1(s.gmsc),
    }


async def list_adv_competitions(db: AsyncSession) -> list[tuple[int, str]]:
    """Distinct adv-eligible ``(year, venue_slug)``, newest year first, LV first."""
    stmt = (
        select(
            SummerLeaguePlayerSeason.year,
            SummerLeaguePlayerSeason.venue_slug,
        )  # type: ignore[call-overload]
        .where(
            SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            SummerLeaguePlayerSeason.adv_eligible.is_(True),  # type: ignore[attr-defined]
            SummerLeaguePlayerSeason.minutes >= DISPLAY_MIN_MINUTES,  # type: ignore[arg-type]
        )
        .distinct()
    )
    pairs = {(int(y), v) for y, v in (await db.execute(stmt)).all()}
    return sorted(pairs, key=lambda k: (-k[0], _VENUE_ORDER.get(k[1], 99)))


def _resolve_competition(
    competitions: list[tuple[int, str]],
    year: Optional[int],
    venue_slug: Optional[str],
) -> Optional[tuple[int, str]]:
    """Pick the competition to show, defaulting toward the latest/marquee pool.

    Honors an exact ``(year, venue)`` match; otherwise falls back to the first
    (marquee-ordered) venue for a requested year, then the latest year for a
    requested venue, then the newest competition overall.
    """
    if not competitions:
        return None
    if (year, venue_slug) in competitions:
        return (year, venue_slug)  # type: ignore[return-value]
    if year is not None:
        for comp in competitions:
            if comp[0] == year:
                return comp
    if venue_slug is not None:
        for comp in competitions:
            if comp[1] == venue_slug:
                return comp
    return competitions[0]


async def get_competition_leaders(
    db: AsyncSession,
    *,
    year: Optional[int] = None,
    venue_slug: Optional[str] = None,
    gates: tuple[tuple[int, int], ...] = OPEN_GATES,
    min_rows: int = 1,
) -> CompetitionLeaders:
    """Fetch one competition's advanced leaderboard rows (unsorted).

    Resolves ``(year, venue_slug)`` to an adv-eligible competition (defaulting to
    the latest when unspecified or unavailable), fetches that pool's rows once,
    then applies ``gates`` rung by rung in Python until anyone qualifies (a
    single competition's pool is small). The caller sorts/paginates.

    An exactly-requested ``(year, venue)`` that has materialized rows but is not
    (yet) adv-eligible — a competition mid-event, typically — is honored rather
    than silently redirected to another pool: its rows are returned with
    ``calibrated=False`` and without the adv-eligibility / display-minutes
    floors, so the box-derived rate columns still populate. The resolved
    competition is always included in ``competitions`` so pickers stay correct.
    """
    competitions = await list_adv_competitions(db)
    resolved = _resolve_competition(competitions, year, venue_slug)

    calibrated = True
    if (
        year is not None
        and venue_slug is not None
        and resolved != (year, venue_slug)
        and await _competition_has_rows(db, year, venue_slug)
    ):
        resolved = (year, venue_slug)
        calibrated = False

    if resolved is None:
        return CompetitionLeaders(
            None,
            None,
            None,
            competitions,
            [],
            min_games=gates[0][0],
            min_minutes=gates[0][1],
        )
    r_year, r_venue = resolved
    if resolved not in competitions:
        competitions = sorted(
            [*competitions, resolved],
            key=lambda k: (-k[0], _VENUE_ORDER.get(k[1], 99)),
        )

    conds: list[object] = [
        SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.year == r_year,  # type: ignore[arg-type]
        SummerLeaguePlayerSeason.venue_slug == r_venue,  # type: ignore[arg-type]
    ]
    if calibrated:
        conds.append(SummerLeaguePlayerSeason.adv_eligible.is_(True))  # type: ignore[attr-defined]

    stmt = (
        select(
            SummerLeaguePlayerSeason,
            PlayerMaster.slug,
            PlayerMaster.display_name,
        )  # type: ignore[call-overload]
        .join(PlayerMaster, PlayerMaster.id == SummerLeaguePlayerSeason.player_id)
        .where(*conds)
    )
    fetched = (await db.execute(stmt)).all()

    floor = DISPLAY_MIN_MINUTES if calibrated else 0.0
    totals = {i: (s.gp, s.minutes) for i, (s, _slug, _name) in enumerate(fetched)}
    qualified, applied_games, applied_minutes = _first_populated_rung(
        gates, floor, totals, min_rows=min_rows
    )
    rows = [
        CompetitionLeaderRow(
            slug=slug,
            name=name or "Player",
            gp=season.gp,
            values=_leader_values(season),
        )
        for i, (season, slug, name) in enumerate(fetched)
        if i in qualified
    ]
    return CompetitionLeaders(
        year=r_year,
        venue_slug=r_venue,
        venue_label=VENUE_LABELS.get(r_venue, r_venue),
        competitions=competitions,
        rows=rows,
        calibrated=calibrated,
        min_games=applied_games,
        min_minutes=applied_minutes,
    )


async def _competition_has_rows(db: AsyncSession, year: int, venue_slug: str) -> bool:
    """True when any materialized season row exists for ``(year, venue_slug)``."""
    stmt = (
        select(SummerLeaguePlayerSeason.id)  # type: ignore[call-overload]
        .where(
            SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            SummerLeaguePlayerSeason.year == year,  # type: ignore[arg-type]
            SummerLeaguePlayerSeason.venue_slug == venue_slug,  # type: ignore[arg-type]
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None


# --------------------------------------------------------------------------- #
# Blended ("All") advanced leaders
# --------------------------------------------------------------------------- #
# When the caller leaves the season and/or venue open, a player's adv-eligible
# pools are rolled into a single line. Cumulative shares sum; rate composites are
# minute-weighted; the shooting percentages pool from raw volume; WS/40 is
# recomputed from the summed shares; GmSc (a per-game score) is game-weighted.
# Blending pool-recalibrated composites across differently-calibrated pools is an
# approximation the "All" scope accepts by design.
_ADV_BLEND_SUM_COLS: tuple[str, ...] = ("ows", "dws", "ws", "vorp")
# T8b (#729): the registry's only additive_share entries are ws/vorp (ows/dws are
# their unregistered components) -- read, not re-derived.
assert rollup_class_matches("ws", RollupClass.ADDITIVE_SHARE)
assert rollup_class_matches("vorp", RollupClass.ADDITIVE_SHARE)

_ADV_BLEND_RATE_COLS: tuple[str, ...] = (
    "per",
    "usg_pct",
    "ortg",
    "drtg",
    "orb_pct",
    "drb_pct",
    "trb_pct",
    "ast_pct",
    "stl_pct",
    "blk_pct",
    "tov_pct",
    "obpm",
    "dbpm",
    "bpm",
)
# ``ortg``/``drtg``/``bpm`` are registry pool_recalibrated composites; minute-
# weighting them here is the same documented cross-pool blend approximation as
# ws82/vorp82 above -- consistent with the registry's class, not a re-derivation.
assert rollup_class_matches("ortg", RollupClass.POOL_RECALIBRATED)
assert rollup_class_matches("drtg", RollupClass.POOL_RECALIBRATED)
assert rollup_class_matches("bpm", RollupClass.POOL_RECALIBRATED)
# **Known, flagged conflict -- not resolved here (T8b / #729 scope discipline).**
# ``usg_pct``/``orb_pct``/``drb_pct``/``trb_pct``/``ast_pct``/``stl_pct``/
# ``blk_pct``/``tov_pct`` are all declared ``RollupClass.RECOMBINABLE`` in the
# registry -- "recompute from summed box totals", not "minute-weighted average".
# Most of them genuinely can't be recomputed here (their formulas need team/
# opponent box totals this blend's season rows don't retain), so the weighted-
# mean approximation is the only option. ``tov_pct`` is the one exception: its
# formula only needs ``tov``/``fga``/``fta``, which this function already sums
# from raw box volume a few lines below for ts_pct/efg_pct/fg3ar/ftr -- so it
# *could* be recombined exactly the same way, and currently isn't. This is a
# deliberate prior decision (career TOV% pooling is minute-weighted), and this
# ticket adopts the registry classification without resolving the conflict it
# surfaces; see the PR/report for the raised discrepancy.
assert all(
    rollup_class_matches(k, RollupClass.RECOMBINABLE)
    for k in (
        "usg_pct",
        "orb_pct",
        "drb_pct",
        "trb_pct",
        "ast_pct",
        "stl_pct",
        "blk_pct",
        "tov_pct",
    )
)


def _blend_leader_values(
    seasons: list[SummerLeaguePlayerSeason],
) -> dict[str, Optional[float]]:
    """Blend a player's adv-eligible pools into one leaderboard line.

    Args:
        seasons: One player's adv-eligible ``SummerLeaguePlayerSeason`` rows
            within the requested scope (a season, a venue, or everything).

    Returns:
        Display values keyed by advanced leader column, per the grain rules
        described above.
    """
    minutes = sum(s.minutes for s in seasons)
    out: dict[str, Optional[float]] = {"min": _round1(minutes)}

    # Cumulative shares add up across pools.
    for col in _ADV_BLEND_SUM_COLS:
        vals = [getattr(s, col) for s in seasons if getattr(s, col) is not None]
        out[col] = _round1(sum(vals)) if vals else None

    # Rate composites are minute-weighted.
    for col in _ADV_BLEND_RATE_COLS:
        out[col] = _round1(
            _weighted_mean([(getattr(s, col), s.minutes) for s in seasons])
        )

    # Shooting percentages and attempt rates pool from raw volume (percentages
    # on a 0-100 scale; 3PAr/FTr as 0-1 fractions, matching the stored columns).
    pts = sum(s.pts for s in seasons)
    fga = sum(s.fga for s in seasons)
    fta = sum(s.fta for s in seasons)
    fgm = sum(s.fgm for s in seasons)
    fg3m = sum((s.fg3m or 0) for s in seasons)
    fg3a = sum((s.fg3a or 0) for s in seasons)
    out["ts_pct"] = ts_pct_line(pts=pts, fga=fga, fta=fta)
    out["efg_pct"] = efg_pct_line(fgm=fgm, fga=fga, fg3m=fg3m)
    out["fg3ar"] = fg3ar_line(fg3a=fg3a, fga=fga)
    out["ftr"] = ftr_line(fga=fga, fta=fta)

    # WS/40 recomputed from summed shares so it stays internally consistent.
    ws_total = out["ws"]
    ws40 = win_shares_per_40(ws_total, minutes) if ws_total is not None else None
    out["ws40"] = round(ws40, 2) if ws40 is not None else None

    # GmSc is a per-game score; blend it game-weighted.
    out["gmsc"] = _round1(_weighted_mean([(s.gmsc, float(s.gp)) for s in seasons]))
    return out


async def get_blended_leaders(
    db: AsyncSession,
    *,
    year: Optional[int] = None,
    venue_slug: Optional[str] = None,
    gates: tuple[tuple[int, int], ...] = OPEN_GATES,
    min_rows: int = 1,
) -> CompetitionLeaders:
    """Blend adv-eligible pools across competitions into one line per player.

    Scope is set by which of ``year`` / ``venue_slug`` are provided: neither is
    the all-time, all-venue career blend; one narrows to that season or venue.
    Players are grouped across their qualifying pools; ``gates`` is applied to
    the blended totals rung by rung (each rung's minutes floor raised to
    :data:`DISPLAY_MIN_MINUTES`) until anyone qualifies — the scope's rows are
    fetched once and re-filtered in Python.

    Returns:
        A :class:`CompetitionLeaders` whose ``rows`` are unsorted; the caller
        sorts and paginates. ``venue_label`` reads "All venues" when unscoped.
    """
    conds: list[object] = [
        SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
        SummerLeaguePlayerSeason.adv_eligible.is_(True),  # type: ignore[attr-defined]
    ]
    if year is not None:
        conds.append(SummerLeaguePlayerSeason.year == year)  # type: ignore[arg-type]
    if venue_slug is not None:
        conds.append(
            SummerLeaguePlayerSeason.venue_slug == venue_slug  # type: ignore[arg-type]
        )

    stmt = (
        select(
            SummerLeaguePlayerSeason,
            PlayerMaster.slug,
            PlayerMaster.display_name,
        )  # type: ignore[call-overload]
        .join(PlayerMaster, PlayerMaster.id == SummerLeaguePlayerSeason.player_id)
        .where(*conds)
    )

    grouped: dict[int, list[SummerLeaguePlayerSeason]] = {}
    meta: dict[int, tuple[Optional[str], str]] = {}
    for season, slug, name in (await db.execute(stmt)).all():
        grouped.setdefault(season.player_id, []).append(season)
        meta[season.player_id] = (slug, name or "Player")

    totals = {
        pid: (sum(s.gp for s in seasons), sum(s.minutes for s in seasons))
        for pid, seasons in grouped.items()
    }
    qualified, applied_games, applied_minutes = _first_populated_rung(
        gates, DISPLAY_MIN_MINUTES, totals, min_rows=min_rows
    )
    rows = [
        CompetitionLeaderRow(
            slug=meta[pid][0],
            name=meta[pid][1],
            gp=totals[pid][0],
            values=_blend_leader_values(grouped[pid]),
        )
        for pid in grouped  # insertion order keeps output deterministic
        if pid in qualified
    ]

    return CompetitionLeaders(
        year=year,
        venue_slug=venue_slug,
        venue_label=VENUE_LABELS.get(venue_slug, venue_slug)
        if venue_slug
        else "All venues",
        competitions=await list_adv_competitions(db),
        rows=rows,
        min_games=applied_games,
        min_minutes=applied_minutes,
    )

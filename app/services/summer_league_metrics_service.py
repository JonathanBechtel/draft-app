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

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason

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
    # Shooting / efficiency.
    ts_pct: Optional[float]
    efg_pct: Optional[float]
    gmsc: Optional[float]
    # Rate.
    usg_pct: Optional[float]
    ast_pct: Optional[float]
    trb_pct: Optional[float]
    # Composites (league-relative, valid only within this pool).
    per: Optional[float]
    ortg: Optional[float]
    drtg: Optional[float]
    net_rtg: Optional[float]
    # Value metrics come in two flavours: a cumulative stat (accrued over the
    # games played) and an 82-game projection (the per-season pace).
    ws: Optional[float]  # cumulative
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
    never as a recalibrated headline.
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
    bpm_avg: Optional[float]
    ws82_avg: Optional[float]
    vorp82_avg: Optional[float]


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


def _career(seasons: list[PlayerMetricSeason]) -> PlayerMetricCareer:
    """Roll adv-eligible seasons into a career line per the grain rules."""
    minutes = sum(s.minutes for s in seasons)
    ws_vals = [s.ws for s in seasons if s.ws is not None]
    vorp_vals = [s.vorp for s in seasons if s.vorp is not None]
    ws_total = sum(ws_vals) if ws_vals else None
    # Recompute WS/40 from the summed shares so it stays internally consistent.
    ws40 = (ws_total / minutes * 40.0) if (ws_total is not None and minutes) else None
    return PlayerMetricCareer(
        adv_pools=len(seasons),
        gp=sum(s.gp for s in seasons),
        minutes=minutes,
        ws=round(ws_total, 1) if ws_total is not None else None,
        vorp=round(sum(vorp_vals), 1) if vorp_vals else None,
        ws40=round(ws40, 2) if ws40 is not None else None,
        per_avg=_round1(_weighted_mean([(s.per, s.minutes) for s in seasons])),
        bpm_avg=_round1(_weighted_mean([(s.bpm, s.minutes) for s in seasons])),
        ws82_avg=_round1(_weighted_mean([(s.ws82, s.minutes) for s in seasons])),
        vorp82_avg=_round1(_weighted_mean([(s.vorp82, s.minutes) for s in seasons])),
    )


def _round1(value: Optional[float]) -> Optional[float]:
    return round(value, 1) if value is not None else None


def _to_season(row: SummerLeaguePlayerSeason) -> PlayerMetricSeason:
    """Map a materialized row to the display dataclass."""
    return PlayerMetricSeason(
        year=row.year,
        venue_slug=row.venue_slug,
        venue_label=VENUE_LABELS.get(row.venue_slug, row.venue_slug),
        venue_abbr=VENUE_ABBR.get(row.venue_slug, row.venue_slug),
        gp=row.gp,
        minutes=row.minutes,
        # Shooting / rate columns are already stored as percentages (e.g. 60.6),
        # not 0-1 fractions, so they only need rounding — not a ×100 rescale.
        ts_pct=_round1(row.ts_pct),
        efg_pct=_round1(row.efg_pct),
        gmsc=_round1(row.gmsc),
        usg_pct=_round1(row.usg_pct),
        ast_pct=_round1(row.ast_pct),
        trb_pct=_round1(row.trb_pct),
        per=_round1(row.per),
        ortg=_round1(row.ortg),
        drtg=_round1(row.drtg),
        net_rtg=_round1(row.net_rtg),
        ws=_round1(row.ws),
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

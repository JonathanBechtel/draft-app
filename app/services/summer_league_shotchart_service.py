"""Read-side service for Summer League shot-chart aggregation.

Aggregates ``SummerLeagueShotEvent`` rows into:

* **Zone heat data** — per-zone FGA/FGM/FG%/freq% plus a pool-average FG% per
  zone (calibrated to the same competition, mirroring the pool-recalibration
  philosophy used for composite metrics).
* **Shot dots** — raw ``(loc_x, loc_y, made)`` triples for the dot-toggle
  overlay.

All functions are stateless: they take an ``AsyncSession`` as their first
parameter and return dataclass DTOs.  No writes occur here.

Shot-zone taxonomy
------------------
Zones follow NBA ``SHOT_ZONE_BASIC`` directly so raw zone fields are preserved
for future re-bucketing.  The "Backcourt" zone is silently excluded from FGA
totals and zone lists because backcourt heaves are noise, not shooting-diet
signal.

Minimum-attempts floor
----------------------
``MIN_FGA_FOR_CHART`` (20 FGA) guards season-level views.  When total FGA falls
below the floor, ``PlayerShotZones.suppressed`` is ``True`` and the zones list
still contains per-zone breakdowns (for the table), but callers MUST NOT render
a heat chart — the sample is too thin to colour reliably.  The table itself
remains safe at any sample size.

Pool baseline
-------------
``pool_fg_pct`` on each zone is the competition-pool average: all shots in the
same ``competition_id``, regardless of player.  It is ``None`` for career
rollups (shots span multiple pools with different baselines, making a single
reference figure misleading).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueShotEvent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Season-level chart suppression threshold.  Below this FGA count the chart
# is unreliable; callers should render the zone table only.
MIN_FGA_FOR_CHART: int = 20

# Excluded from all aggregation: backcourt heaves carry no shooting-diet signal.
_EXCLUDED_ZONES: frozenset[str] = frozenset({"Backcourt"})

# Canonical display order for the 6 included NBA shot-zone-basic values.
_ZONE_ORDER: dict[str, int] = {
    "Restricted Area": 0,
    "In The Paint (Non-RA)": 1,
    "Mid-Range": 2,
    "Left Corner 3": 3,
    "Right Corner 3": 4,
    "Above the Break 3": 5,
}


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class ShotZoneRow:
    """Aggregated shooting stats for one NBA shot-zone-basic bucket.

    All raw zone fields (``shot_zone_basic``, plus the caller-visible label)
    are stored so future re-bucketing into a simpler taxonomy is possible
    without re-querying.
    """

    shot_zone_basic: str
    fga: int
    fgm: int
    fg_pct: Optional[float]  # None when fga == 0
    freq_pct: float  # fraction of filtered total FGA (0–1)
    pool_fg_pct: Optional[float]  # competition-pool baseline; None for career


@dataclass
class PlayerShotZones:
    """Shot-zone aggregation for a player, optionally scoped to a competition.

    When ``competition_id`` is ``None`` this is a career rollup: shot counts
    sum additively across all competitions and ``pool_fg_pct`` on each zone
    is ``None`` (no meaningful single-pool reference).

    ``suppressed`` is ``True`` when ``total_fga < MIN_FGA_FOR_CHART``.  In
    that case callers MUST render the table only and suppress the heat chart.
    The ``zones`` list is still populated so the table can display raw counts.
    """

    player_id: int
    competition_id: Optional[int]
    total_fga: int
    suppressed: bool
    zones: list[ShotZoneRow]


@dataclass
class ShotDot:
    """A single raw shot event for the dot-toggle overlay."""

    loc_x: int
    loc_y: int
    made: bool


@dataclass
class PlayerShotDots:
    """Raw shot locations for a player within one competition."""

    player_id: int
    competition_id: int
    dots: list[ShotDot]


@dataclass
class GameShotZones:
    """Shot-zone aggregation for a game, optionally filtered.

    ``team_entry_id`` and ``player_id`` are the filter applied; both may be
    ``None`` (all shots in the game).  Game-scope queries use the same zone
    taxonomy and suppression logic as the player view.
    """

    game_id: int
    team_entry_id: Optional[int]
    player_id: Optional[int]
    total_fga: int
    suppressed: bool
    zones: list[ShotZoneRow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_pct(made: int, attempted: int) -> Optional[float]:
    """Return FG% (0–1) or ``None`` when ``attempted`` is zero."""
    return made / attempted if attempted else None


def _build_zone_rows(
    shot_rows: list[tuple[str, int, int]],
    pool_map: dict[str, Optional[float]],
) -> tuple[int, list[ShotZoneRow]]:
    """Turn aggregated (zone, fgm, fga) tuples into sorted ``ShotZoneRow`` list.

    Args:
        shot_rows: List of ``(zone_label, fgm, fga)`` from the DB query.
        pool_map: Per-zone pool FG% baselines; use ``{}`` for career rollups.

    Returns:
        ``(total_fga, zones)`` where zones are sorted by ``_ZONE_ORDER``.
    """
    total_fga = sum(row[2] for row in shot_rows)
    rows: list[ShotZoneRow] = []
    for zone, fgm, fga in shot_rows:
        rows.append(
            ShotZoneRow(
                shot_zone_basic=zone,
                fga=fga,
                fgm=fgm,
                fg_pct=_safe_pct(fgm, fga),
                freq_pct=fga / total_fga if total_fga else 0.0,
                pool_fg_pct=pool_map.get(zone),
            )
        )
    rows.sort(key=lambda r: _ZONE_ORDER.get(r.shot_zone_basic, 99))
    return total_fga, rows


async def _fetch_zone_agg(
    session: AsyncSession,
    *,
    player_id: Optional[int] = None,
    competition_id: Optional[int] = None,
    game_id: Optional[int] = None,
    team_entry_id: Optional[int] = None,
) -> list[tuple[str, int, int]]:
    """Query zone-level FGM/FGA totals from ``SummerLeagueShotEvent``.

    Filters by whichever of the optional ID arguments are supplied.  Excludes
    ``_EXCLUDED_ZONES`` and rows where ``shot_zone_basic`` is ``NULL``.

    Returns:
        List of ``(zone_label, fgm, fga)`` tuples.
    """
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeagueShotEvent.shot_zone_basic,
            func.sum(cast(SummerLeagueShotEvent.made, Integer)).label("fgm"),
            func.count().label("fga"),
        )
        .where(SummerLeagueShotEvent.shot_zone_basic.isnot(None))  # type: ignore[union-attr]
        .where(
            SummerLeagueShotEvent.shot_zone_basic.notin_(list(_EXCLUDED_ZONES))  # type: ignore[union-attr]
        )
        .group_by(SummerLeagueShotEvent.shot_zone_basic)
    )
    if player_id is not None:
        stmt = stmt.where(
            SummerLeagueShotEvent.player_id == player_id  # type: ignore[arg-type]
        )
    if competition_id is not None:
        stmt = stmt.where(
            SummerLeagueShotEvent.competition_id == competition_id  # type: ignore[arg-type]
        )
    if game_id is not None:
        stmt = stmt.where(
            SummerLeagueShotEvent.game_id == game_id  # type: ignore[arg-type]
        )
    if team_entry_id is not None:
        stmt = stmt.where(
            SummerLeagueShotEvent.team_entry_id == team_entry_id  # type: ignore[arg-type]
        )
    result = await session.execute(stmt)
    return [(row[0], int(row[1] or 0), int(row[2])) for row in result.all()]


async def _fetch_pool_baseline(
    session: AsyncSession,
    competition_id: int,
) -> dict[str, Optional[float]]:
    """Compute per-zone FG% across all shots in a competition pool.

    This is the "league average" reference used to colour each zone cell.
    All players in the competition contribute, making it the true pool baseline
    (consistent with the composite-metrics recalibration philosophy).

    Args:
        session: Async DB session.
        competition_id: The competition whose shots form the pool.

    Returns:
        Dict mapping ``shot_zone_basic`` → pool FG% (``None`` when no shots).
    """
    rows = await _fetch_zone_agg(session, competition_id=competition_id)
    return {zone: _safe_pct(fgm, fga) for zone, fgm, fga in rows}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_player_shot_zones(
    session: AsyncSession,
    player_id: int,
    competition_id: Optional[int] = None,
) -> PlayerShotZones:
    """Return per-zone shooting breakdown for a player.

    When ``competition_id`` is provided, results are scoped to that
    competition and ``pool_fg_pct`` reflects the competition-pool baseline.
    When ``competition_id`` is ``None``, all competitions are summed (career
    rollup) and ``pool_fg_pct`` is ``None`` on every zone.

    ``suppressed`` is ``True`` when ``total_fga < MIN_FGA_FOR_CHART``.
    Callers MUST suppress the heat chart in that case; the zone table is always
    safe to display.

    Args:
        session: Async DB session.
        player_id: Canonical ``PlayerMaster.id``.
        competition_id: Optional competition scope; ``None`` = career.

    Returns:
        :class:`PlayerShotZones` DTO.
    """
    shot_rows = await _fetch_zone_agg(
        session, player_id=player_id, competition_id=competition_id
    )

    if competition_id is not None:
        pool_map: dict[str, Optional[float]] = await _fetch_pool_baseline(
            session, competition_id
        )
    else:
        pool_map = {}

    total_fga, zones = _build_zone_rows(shot_rows, pool_map)
    return PlayerShotZones(
        player_id=player_id,
        competition_id=competition_id,
        total_fga=total_fga,
        suppressed=total_fga < MIN_FGA_FOR_CHART,
        zones=zones,
    )


async def get_player_shot_dots(
    session: AsyncSession,
    player_id: int,
    competition_id: int,
) -> PlayerShotDots:
    """Return raw shot locations for the dot-toggle overlay.

    Only shots with non-null ``loc_x`` / ``loc_y`` are included; rows missing
    coordinates are silently dropped (they cannot be plotted).

    Args:
        session: Async DB session.
        player_id: Canonical ``PlayerMaster.id``.
        competition_id: Competition scope (required — dots span one pool).

    Returns:
        :class:`PlayerShotDots` with ``dots`` list (may be empty).
    """
    stmt = (
        select(  # type: ignore[call-overload]
            SummerLeagueShotEvent.loc_x,
            SummerLeagueShotEvent.loc_y,
            SummerLeagueShotEvent.made,
        )
        .where(
            SummerLeagueShotEvent.player_id == player_id  # type: ignore[arg-type]
        )
        .where(
            SummerLeagueShotEvent.competition_id == competition_id  # type: ignore[arg-type]
        )
        .where(SummerLeagueShotEvent.loc_x.isnot(None))  # type: ignore[union-attr]
        .where(SummerLeagueShotEvent.loc_y.isnot(None))  # type: ignore[union-attr]
        .order_by(SummerLeagueShotEvent.id)
    )
    result = await session.execute(stmt)
    dots = [
        ShotDot(loc_x=row[0], loc_y=row[1], made=bool(row[2])) for row in result.all()
    ]
    return PlayerShotDots(
        player_id=player_id,
        competition_id=competition_id,
        dots=dots,
    )


async def get_game_shot_zones(
    session: AsyncSession,
    game_id: int,
    team_entry_id: Optional[int] = None,
    player_id: Optional[int] = None,
) -> GameShotZones:
    """Return per-zone shooting breakdown for a game.

    Optionally filter to a single team (``team_entry_id``) or a single player
    (``player_id``).  When both are supplied both filters apply.

    Pool baseline for game-scoped zones is not supplied (a single game has far
    too few shots to form a meaningful pool); ``pool_fg_pct`` is ``None`` on
    all zone rows.

    ``suppressed`` is ``True`` when ``total_fga < MIN_FGA_FOR_CHART``.

    Args:
        session: Async DB session.
        game_id: ``SummerLeagueGame.id``.
        team_entry_id: Optional ``SummerLeagueTeamEntry.id`` filter.
        player_id: Optional canonical ``PlayerMaster.id`` filter.

    Returns:
        :class:`GameShotZones` DTO.
    """
    shot_rows = await _fetch_zone_agg(
        session,
        game_id=game_id,
        team_entry_id=team_entry_id,
        player_id=player_id,
    )
    total_fga, zones = _build_zone_rows(shot_rows, {})
    return GameShotZones(
        game_id=game_id,
        team_entry_id=team_entry_id,
        player_id=player_id,
        total_fga=total_fga,
        suppressed=total_fga < MIN_FGA_FOR_CHART,
        zones=zones,
    )

"""Materialized Summer League advanced-metrics schemas.

These tables are **derived and rebuildable**, not sources of truth: a rebuild job
recomputes them from the raw box logs (`summer_league_player_game_logs` +
`summer_league_team_game_logs`). Keeping them materialized lets the public pages
read pre-computed PER / ratings / Win Shares / BPM without re-deriving the
league-relative context per request.

Three tables:

* :class:`SummerLeagueMetricModel` — the global, league-fit coefficients
  (Pythagorean exponent, BPM regression weights) with a version stamp so a refit
  is auditable.
* :class:`SummerLeagueMetricContext` — one ``LeagueContext`` row per competition
  (year + venue), holding the recalibration constants and the pool's eligibility
  for league-relative metrics.
* :class:`SummerLeaguePlayerSeason` — one materialized row per (player,
  competition) with box totals and every computed metric.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class SummerLeagueMetricModel(SQLModel, table=True):  # type: ignore[call-arg]
    """One versioned fit of the league-wide SL metric coefficients."""

    __tablename__ = "summer_league_metric_models"
    __table_args__ = (
        UniqueConstraint(
            "model_version", name="uq_summer_league_metric_models_version"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    model_version: str = Field(nullable=False)
    # Win Shares points-to-wins, derived from the SL Pythagorean exponent.
    pyth_exponent: float = Field(nullable=False)
    ws_ppw_coeff: float = Field(nullable=False)  # == 4 / pyth_exponent
    pyth_n_teams: int = Field(default=0, nullable=False)
    # SL-native BPM regression (box per-100 -> plus-minus).
    bpm_intercept: float = Field(nullable=False)
    bpm_r2: float = Field(nullable=False)
    bpm_n_fit: int = Field(default=0, nullable=False)
    bpm_replacement: float = Field(default=-2.0, nullable=False)
    # Coefficient name -> weight (per-100 possessions).
    bpm_coefficients: dict[str, float] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    is_active: bool = Field(default=True, nullable=False)
    fitted_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueMetricContext(SQLModel, table=True):  # type: ignore[call-arg]
    """Recalibration constants for one (year, venue) competition pool."""

    __tablename__ = "summer_league_metric_contexts"
    __table_args__ = (
        UniqueConstraint(
            "competition_id",
            name="uq_summer_league_metric_contexts_competition",
        ),
        Index("ix_summer_league_metric_contexts_year_venue", "year", "venue_slug"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_competitions.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    year: int = Field(nullable=False)
    venue_slug: str = Field(nullable=False)
    # League-relative recalibration constants (built from summed totals).
    pace: float = Field(default=0.0, nullable=False)
    pts_per_poss: float = Field(default=0.0, nullable=False)
    ppg: float = Field(default=0.0, nullable=False)
    factor: float = Field(default=0.0, nullable=False)
    vop: float = Field(default=0.0, nullable=False)
    drb_pct: float = Field(default=0.0, nullable=False)
    aper_scalar: float = Field(default=0.0, nullable=False)  # minute-weighted mean aPER
    # Pool size / completeness; gates whether league-relative metrics are valid.
    n_team_games: int = Field(default=0, nullable=False)
    n_complete_games: int = Field(default=0, nullable=False)
    adv_eligible: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeaguePlayerSeason(SQLModel, table=True):  # type: ignore[call-arg]
    """Materialized per-(player, competition) box totals and computed metrics.

    ``adv_eligible`` mirrors the pool's flag: when ``False`` the league-relative
    composite columns (PER/ORtg/DRtg/WS/BPM/...) are ``None`` and only the box,
    shooting, and per-game columns are populated.
    """

    __tablename__ = "summer_league_player_seasons"
    __table_args__ = (
        UniqueConstraint(
            "competition_id",
            "player_id",
            name="uq_summer_league_player_seasons_competition_player",
        ),
        Index("ix_summer_league_player_seasons_player_id", "player_id"),
        Index("ix_summer_league_player_seasons_year_venue", "year", "venue_slug"),
        Index("ix_summer_league_player_seasons_competition", "competition_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_competitions.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    player_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("players_master.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    primary_team_entry_id: Optional[int] = Field(
        default=None, foreign_key="summer_league_team_entries.id"
    )
    year: int = Field(nullable=False)
    venue_slug: str = Field(nullable=False)

    # Sample.
    gp: int = Field(default=0, nullable=False)
    minutes: float = Field(default=0.0, nullable=False)

    # Box totals (so per-game / per-36 / per-100 stay derivable downstream).
    fgm: int = Field(default=0, nullable=False)
    fga: int = Field(default=0, nullable=False)
    fg3m: int = Field(default=0, nullable=False)
    fg3a: int = Field(default=0, nullable=False)
    ftm: int = Field(default=0, nullable=False)
    fta: int = Field(default=0, nullable=False)
    oreb: int = Field(default=0, nullable=False)
    dreb: int = Field(default=0, nullable=False)
    reb: int = Field(default=0, nullable=False)
    ast: int = Field(default=0, nullable=False)
    stl: int = Field(default=0, nullable=False)
    blk: int = Field(default=0, nullable=False)
    tov: int = Field(default=0, nullable=False)
    pf: int = Field(default=0, nullable=False)
    pts: int = Field(default=0, nullable=False)
    plus_minus: int = Field(default=0, nullable=False)

    # Shooting / efficiency (player-only).
    ts_pct: Optional[float] = Field(default=None)
    efg_pct: Optional[float] = Field(default=None)
    # fg3ar: 3PA / FGA derived from box data (fraction, 0–1, rounded to 3 dp).
    # three_rate: same concept but derived from shot-chart zone data; the two
    # will differ when shot-chart coverage is incomplete (unresolved players,
    # missing game files) and can be used as a cross-check.  Both are fractions.
    fg3ar: Optional[float] = Field(default=None)
    ftr: Optional[float] = Field(default=None)
    gmsc: Optional[float] = Field(default=None)  # per-game Game Score

    # Shot-diet rates derived from SummerLeagueShotEvent zone data.
    # Populated only when shot-chart data exists for the player-competition;
    # NULL otherwise (never fabricated from box data).
    # Stored as fractions (0.0–1.0), rounded to 4 decimal places, consistent
    # with fg3ar / ftr.  Backcourt shots excluded from the denominator.
    # Zone mapping (NBA SHOT_ZONE_BASIC):
    #   rim_rate     — Restricted Area
    #   mid_rate     — In The Paint (Non-RA) + Mid-Range
    #   three_rate   — Left Corner 3 + Right Corner 3 + Above the Break 3
    #   corner3_rate — Left Corner 3 + Right Corner 3 (subset of three_rate)
    rim_rate: Optional[float] = Field(default=None)
    mid_rate: Optional[float] = Field(default=None)
    three_rate: Optional[float] = Field(default=None)
    corner3_rate: Optional[float] = Field(default=None)

    # Assisted-FG counts derived from play-by-play events (PBP era only).
    # ast_fgm  — made FGs where a recorded assister (person2_id) exists.
    # unast_fgm — made FGs with no recorded assister.
    # Both are NULL when no PBP made-FG events exist for the player-competition;
    # never fabricated from box data.  Sum across competitions for career totals.
    ast_fgm: Optional[int] = Field(default=None)
    unast_fgm: Optional[int] = Field(default=None)

    # Rate stats (need team + opponent context).
    usg_pct: Optional[float] = Field(default=None)
    ast_pct: Optional[float] = Field(default=None)
    orb_pct: Optional[float] = Field(default=None)
    drb_pct: Optional[float] = Field(default=None)
    trb_pct: Optional[float] = Field(default=None)
    stl_pct: Optional[float] = Field(default=None)
    blk_pct: Optional[float] = Field(default=None)
    tov_pct: Optional[float] = Field(default=None)

    # Possession / pace.
    pace: Optional[float] = Field(default=None)
    pts_per100: Optional[float] = Field(default=None)

    # Composites (league-relative; null when the pool is not adv-eligible).
    per: Optional[float] = Field(default=None)
    ortg: Optional[float] = Field(default=None)
    drtg: Optional[float] = Field(default=None)
    net_rtg: Optional[float] = Field(default=None)
    ows: Optional[float] = Field(default=None)
    dws: Optional[float] = Field(default=None)
    ws: Optional[float] = Field(default=None)  # cumulative Win Shares
    ws40: Optional[float] = Field(default=None)
    ws82: Optional[float] = Field(default=None)  # WS projected to an 82-game season
    obpm: Optional[float] = Field(default=None)
    dbpm: Optional[float] = Field(default=None)
    bpm: Optional[float] = Field(default=None)
    vorp: Optional[float] = Field(default=None)  # cumulative VORP (accrued)
    vorp82: Optional[float] = Field(default=None)  # VORP projected to 82 games

    adv_eligible: bool = Field(default=False, nullable=False)
    model_version: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

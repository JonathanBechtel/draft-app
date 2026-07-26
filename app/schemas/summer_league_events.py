"""Summer League event-grain tables: shot attempts and play-by-play events.

Split out of ``app.schemas.summer_league`` (#681). Those two tables are the
event grain — one row per shot attempt / per play-by-play event, both keyed on
``nba_stats_game_id`` and both parsed straight from a per-game NBA Stats
payload — while everything left behind in ``summer_league`` is competition,
game, roster or aggregate grain. They are also the tables that grow fastest, so
they attract the most index work; keeping them here means that work no longer
pushes an already-oversized module further over the file-size ratchet
(``docs/plans/programmatic-code-discipline.md`` §1.4).

``app.schemas.summer_league`` re-exports both names so the ~29 existing import
sites are unaffected; import from either module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, UniqueConstraint, text
from sqlmodel import Field, SQLModel


class SummerLeagueShotEvent(SQLModel, table=True):  # type: ignore[call-arg]
    """One row per shot attempt parsed from shotchartdetail JSON."""

    __tablename__ = "summer_league_shot_events"
    __table_args__ = (
        UniqueConstraint(
            "nba_stats_game_id",
            "nba_stats_game_event_id",
            name="uq_summer_league_shot_events_game_event",
        ),
        Index(
            "ix_summer_league_shot_events_player_competition",
            "player_id",
            "competition_id",
        ),
        Index("ix_summer_league_shot_events_game_id", "game_id"),
        # competition-leading index for the pool-baseline aggregation
        # (_fetch_pool_baseline groups by zone within a single competition).
        Index(
            "ix_summer_league_shot_events_competition_zone",
            "competition_id",
            "shot_zone_basic",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(foreign_key="summer_league_games.id")
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    team_entry_id: int = Field(foreign_key="summer_league_team_entries.id")
    source_player_id: int = Field(foreign_key="summer_league_source_players.id")
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    nba_stats_person_id: str = Field(nullable=False)
    nba_stats_game_id: str = Field(nullable=False)
    nba_stats_game_event_id: int = Field(nullable=False)
    period: Optional[int] = Field(default=None)
    minutes_remaining: Optional[int] = Field(default=None)
    seconds_remaining: Optional[int] = Field(default=None)
    loc_x: Optional[int] = Field(default=None)
    loc_y: Optional[int] = Field(default=None)
    shot_distance: Optional[int] = Field(default=None)
    shot_type: Optional[str] = Field(default=None)
    shot_zone_basic: Optional[str] = Field(default=None)
    shot_zone_area: Optional[str] = Field(default=None)
    shot_zone_range: Optional[str] = Field(default=None)
    action_type: Optional[str] = Field(default=None)
    made: bool = Field(nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeaguePlayByPlayEvent(SQLModel, table=True):  # type: ignore[call-arg]
    """One row per play-by-play event parsed from playbyplayv2 JSON."""

    __tablename__ = "summer_league_play_by_play_events"
    __table_args__ = (
        UniqueConstraint(
            "nba_stats_game_id",
            "event_num",
            name="uq_summer_league_pbp_events_game_event_num",
        ),
        Index(
            "ix_summer_league_pbp_events_game_period_event",
            "game_id",
            "period",
            "event_num",
        ),
        # Competition-leading index for the Competition Context rebuild's PBP
        # coverage certification (#643): _load_pbp filters/groups by
        # competition_id, but the index above is led by game_id, so at prod
        # volume (44 competitions / ~40k rows, one competition alone holding
        # ~80% of them) that query falls back to a Seq Scan over the whole
        # table. This mirrors the competition-leading indexes already present
        # on team/player game logs, participation, and shot events.
        Index(
            "ix_summer_league_pbp_events_competition_game",
            "competition_id",
            "game_id",
        ),
        # Player-leading indexes for the three participant FKs (#681). The merge
        # path reassigns each column independently and the safe-delete guard
        # counts each one, so without these every stub deletion and every player
        # merge sequentially Seq-Scans the whole table three times — and so does
        # the RESTRICT FK check Postgres runs when the parent row is deleted.
        # Partial: person2/person3 are NULL on the large majority of events, and
        # `col = :id` implies `col IS NOT NULL`, so the planner can still use them.
        Index(
            "ix_summer_league_pbp_events_person1",
            "person1_id",
            postgresql_where=text("person1_id IS NOT NULL"),
        ),
        Index(
            "ix_summer_league_pbp_events_person2",
            "person2_id",
            postgresql_where=text("person2_id IS NOT NULL"),
        ),
        Index(
            "ix_summer_league_pbp_events_person3",
            "person3_id",
            postgresql_where=text("person3_id IS NOT NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(foreign_key="summer_league_games.id")
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    nba_stats_game_id: str = Field(nullable=False)
    event_num: int = Field(nullable=False)
    period: Optional[int] = Field(default=None)
    clock: Optional[str] = Field(default=None)
    event_msg_type: Optional[int] = Field(default=None)
    home_score: Optional[int] = Field(default=None)
    away_score: Optional[int] = Field(default=None)
    score_margin: Optional[int] = Field(default=None)
    person1_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    person1_nba_id: Optional[str] = Field(default=None)
    person2_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    person2_nba_id: Optional[str] = Field(default=None)
    person3_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    person3_nba_id: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

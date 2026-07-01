"""Read-only reconciliation between the announced roster and box-score appearances.

For a given Summer League competition, this flags:

- **Announced but never played** (DNP/cut) — participations whose current
  affiliation was sourced from the NBA.com roster feed, but who never post a
  box-score line in the competition.
- **Played but never announced** (late-adds) — distinct box-score source
  players with no roster-sourced participation.

Set arithmetic runs over the ``source_player_id`` identity space rather than
``participation_id``: game-log rows written before the T4 backfill (B1)
carry a NULL ``participation_id``, so joining on it would silently drop
legitimate box-score appearances. See the roster-foundation participation
backfill notes for background.

This module performs no writes — it is purely a read-side diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import PlayerAffiliation
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)

# Affiliation source stamped by the roster loader (app/services/summer_league/roster_ingest.py).
ROSTER_SOURCE = "nba_summer_league_roster"


@dataclass
class ReconcileEntry:
    """One flagged player in a reconcile report.

    Args:
        source_player_id: PK of the ``SummerLeagueSourcePlayer`` row.
        name: Canonical ``PlayerMaster.display_name`` if resolved, else the
            raw NBA.com roster/box-score name.
        team_name: ``SummerLeagueTeamEntry.raw_team_name`` for the team the
            entry is associated with.
    """

    source_player_id: int
    name: str
    team_name: str


@dataclass
class RosterReconcileReport:
    """Announced-vs-played reconciliation for one competition.

    Args:
        competition_id: PK of the ``SummerLeagueCompetition`` row.
        total_announced: Distinct roster-sourced participants for the competition.
        total_played: Distinct box-score source players for the competition.
        announced_and_played: Count present in both sets (excluded from the
            two flagged lists below).
        announced_not_played: Roster-sourced participants with zero box-score
            rows (DNP/cut candidates).
        played_not_announced: Box-score source players with no roster-sourced
            participation (late-add candidates).
    """

    competition_id: int
    total_announced: int
    total_played: int
    announced_and_played: int
    announced_not_played: list[ReconcileEntry]
    played_not_announced: list[ReconcileEntry]


async def reconcile_competition(
    db: AsyncSession, competition_id: int
) -> RosterReconcileReport:
    """Reconcile the announced roster against box-score appearances for a competition.

    Read-only: issues only ``SELECT`` statements, never mutates state.

    Args:
        db: Async database session.
        competition_id: PK of the ``SummerLeagueCompetition`` row to reconcile.

    Returns:
        A ``RosterReconcileReport`` with totals and the two flagged entry lists.
    """
    # 1. Announced: participations whose *current* affiliation was sourced
    # from the NBA.com roster feed, for this competition.
    announced_result = await db.execute(
        select(SummerLeagueParticipation)
        .join(
            PlayerAffiliation,
            SummerLeagueParticipation.affiliation_id == PlayerAffiliation.id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
            PlayerAffiliation.source == ROSTER_SOURCE,  # type: ignore[arg-type]
        )
    )
    announced_participations = list(announced_result.scalars().all())

    # Dedupe to one representative participation per source_player_id (lowest
    # id wins) — a player can in principle carry multiple stints/teams.
    announced_by_source_player_id: dict[int, SummerLeagueParticipation] = {}
    for participation in sorted(announced_participations, key=lambda p: p.id or 0):
        announced_by_source_player_id.setdefault(
            participation.source_player_id, participation
        )

    # 2. Played: distinct source players appearing in a box score for this
    # competition. Joined on (competition_id, source_player_id) deliberately —
    # participation_id is NULL on pre-B1 rows and must not gate this query.
    played_result = await db.execute(
        select(  # type: ignore[call-overload]
            SummerLeaguePlayerGameLog.source_player_id,
            SummerLeaguePlayerGameLog.team_entry_id,
            SummerLeaguePlayerGameLog.player_id,
            SummerLeaguePlayerGameLog.id,
        ).where(
            SummerLeaguePlayerGameLog.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    played_rows = played_result.all()

    # Dedupe to one representative row per source_player_id (lowest game-log
    # id wins) so a multi-game player doesn't produce ambiguous team/name data.
    played_by_source_player_id: dict[int, tuple[int, Optional[int]]] = {}
    for source_player_id, team_entry_id, player_id, log_id in sorted(
        played_rows, key=lambda row: row[3] or 0
    ):
        played_by_source_player_id.setdefault(
            source_player_id, (team_entry_id, player_id)
        )

    announced_ids = set(announced_by_source_player_id.keys())
    played_ids = set(played_by_source_player_id.keys())

    announced_and_played_ids = announced_ids & played_ids
    announced_not_played_ids = announced_ids - played_ids
    played_not_announced_ids = played_ids - announced_ids

    # 3. Batch-fetch display data for the flagged entries: source players
    # (raw name fallback), canonical players (preferred display name), and
    # team entries (raw team name).
    all_source_player_ids = announced_ids | played_ids
    source_players_by_id: dict[int, SummerLeagueSourcePlayer] = {}
    if all_source_player_ids:
        sp_result = await db.execute(
            select(SummerLeagueSourcePlayer).where(
                SummerLeagueSourcePlayer.id.in_(all_source_player_ids)  # type: ignore[union-attr]
            )
        )
        for source_player in sp_result.scalars().all():
            if source_player.id is not None:
                source_players_by_id[source_player.id] = source_player

    all_team_entry_ids = {
        participation.team_entry_id
        for participation in announced_by_source_player_id.values()
    } | {team_entry_id for team_entry_id, _ in played_by_source_player_id.values()}
    team_entries_by_id: dict[int, SummerLeagueTeamEntry] = {}
    if all_team_entry_ids:
        team_result = await db.execute(
            select(SummerLeagueTeamEntry).where(
                SummerLeagueTeamEntry.id.in_(all_team_entry_ids)  # type: ignore[union-attr]
            )
        )
        for team_entry in team_result.scalars().all():
            if team_entry.id is not None:
                team_entries_by_id[team_entry.id] = team_entry

    all_player_ids = {
        participation.player_id
        for participation in announced_by_source_player_id.values()
        if participation.player_id is not None
    } | {
        player_id
        for _, player_id in played_by_source_player_id.values()
        if player_id is not None
    }
    players_by_id: dict[int, PlayerMaster] = {}
    if all_player_ids:
        player_result = await db.execute(
            select(PlayerMaster).where(
                PlayerMaster.id.in_(all_player_ids)  # type: ignore[union-attr]
            )
        )
        for player in player_result.scalars().all():
            if player.id is not None:
                players_by_id[player.id] = player

    def _name(player_id: Optional[int], source_player_id: int) -> str:
        if player_id is not None:
            player = players_by_id.get(player_id)
            if player is not None and player.display_name:
                return player.display_name
        source_player = source_players_by_id.get(source_player_id)
        return source_player.raw_player_name if source_player else ""

    def _team_name(team_entry_id: int) -> str:
        team_entry = team_entries_by_id.get(team_entry_id)
        return team_entry.raw_team_name if team_entry else ""

    announced_not_played = [
        ReconcileEntry(
            source_player_id=source_player_id,
            name=_name(
                announced_by_source_player_id[source_player_id].player_id,
                source_player_id,
            ),
            team_name=_team_name(
                announced_by_source_player_id[source_player_id].team_entry_id
            ),
        )
        for source_player_id in sorted(announced_not_played_ids)
    ]

    played_not_announced = [
        ReconcileEntry(
            source_player_id=source_player_id,
            name=_name(
                played_by_source_player_id[source_player_id][1], source_player_id
            ),
            team_name=_team_name(played_by_source_player_id[source_player_id][0]),
        )
        for source_player_id in sorted(played_not_announced_ids)
    ]

    return RosterReconcileReport(
        competition_id=competition_id,
        total_announced=len(announced_ids),
        total_played=len(played_ids),
        announced_and_played=len(announced_and_played_ids),
        announced_not_played=announced_not_played,
        played_not_announced=played_not_announced,
    )

"""Integration tests for the read-only roster reconcile service.

Exercises ``reconcile_competition`` against a real Postgres test schema and
asserts the invariants from the T1 ticket/test plan:

1. ``test_classifies_announced_played_and_late_add`` — an announced-but-DNP
   player, an announced+played player, and a played-but-unannounced (late-add)
   player are classified into the correct buckets with correct totals.
2. ``test_null_participation_id_still_counts_as_played`` — a game-log row with
   a NULL ``participation_id`` still counts toward ``total_played`` (the join
   is on ``(competition_id, source_player_id)``, not ``participation_id``).
3. ``test_read_only_no_mutations_across_two_runs`` — running the reconcile
   twice in a row never mutates any row (row counts and content are stable).
4. ``test_empty_competition`` — a competition with no participations and no
   game logs reconciles to all-zero totals and empty lists.
5. ``test_unresolved_only_source_players`` — participations/game logs whose
   source players are still unresolved (no ``player_id``) still reconcile,
   falling back to the raw name.

Requires ``TEST_DATABASE_URL``, ``PYTEST_ALLOW_DB=1``, and empty
``GEMINI_API_KEY``/``GEMINI_SUMMARIZATION_API_KEY``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.roster_reconcile import (
    ROSTER_SOURCE,
    reconcile_competition,
)

T0 = datetime(2026, 7, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_competition(db: AsyncSession) -> int:
    """Create and flush a minimal ``SummerLeagueCompetition`` row; return its id."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2026 Las Vegas Summer League",
    )
    db.add(competition)
    await db.flush()
    return competition.id  # type: ignore[return-value]


async def _make_team_entry(
    db: AsyncSession, competition_id: int, nba_stats_team_id: str = "1610612739"
) -> int:
    """Create and flush a minimal ``SummerLeagueTeamEntry`` row; return its id."""
    team_entry = SummerLeagueTeamEntry(
        competition_id=competition_id,
        nba_stats_team_id=nba_stats_team_id,
        raw_team_name="Cleveland Cavaliers",
        team_slug="cavaliers",
    )
    db.add(team_entry)
    await db.flush()
    return team_entry.id  # type: ignore[return-value]


async def _make_source_player(
    db: AsyncSession, nba_stats_person_id: str, raw_player_name: str
) -> SummerLeagueSourcePlayer:
    """Create and flush a minimal ``SummerLeagueSourcePlayer`` row."""
    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=nba_stats_person_id,
        raw_player_name=raw_player_name,
        normalized_name=raw_player_name.lower().replace(" ", ""),
    )
    db.add(source_player)
    await db.flush()
    return source_player


async def _make_game(
    db: AsyncSession, competition_id: int, nba_stats_game_id: str
) -> int:
    """Create and flush a minimal ``SummerLeagueGame`` row; return its id."""
    game = SummerLeagueGame(
        competition_id=competition_id,
        nba_stats_game_id=nba_stats_game_id,
        status=SummerLeagueGameStatus.FINAL,
    )
    db.add(game)
    await db.flush()
    return game.id  # type: ignore[return-value]


async def _announce(
    db: AsyncSession,
    competition_id: int,
    team_entry_id: int,
    source_player: SummerLeagueSourcePlayer,
    player_id: Optional[int] = None,
) -> SummerLeagueParticipation:
    """Create a roster-sourced ANNOUNCED affiliation + participation bridge row."""
    affiliation = PlayerAffiliation(
        player_id=player_id,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.ANNOUNCED,
        recorded_at=T0,
        source=ROSTER_SOURCE,
    )
    db.add(affiliation)
    await db.flush()

    participation = SummerLeagueParticipation(
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player.id,
        player_id=player_id,
        affiliation_id=affiliation.id,
        stint_no=1,
        roster_status=AffiliationStatus.ANNOUNCED,
    )
    db.add(participation)
    await db.flush()
    return participation


async def _game_log(
    db: AsyncSession,
    competition_id: int,
    game_id: int,
    team_entry_id: int,
    source_player: SummerLeagueSourcePlayer,
    *,
    participation_id: Optional[int],
    player_id: Optional[int] = None,
) -> SummerLeaguePlayerGameLog:
    """Create a box-score player-game-log row."""
    log = SummerLeaguePlayerGameLog(
        competition_id=competition_id,
        game_id=game_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player.id,
        player_id=player_id,
        participation_id=participation_id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        raw_player_name=source_player.raw_player_name,
    )
    db.add(log)
    await db.flush()
    return log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifies_announced_played_and_late_add(
    db_session: AsyncSession,
) -> None:
    """Announced-DNP, announced+played, and late-add players classify correctly.

    Seeds three source players against one competition/team:
    - P1: announced, no game logs (DNP/cut candidate).
    - P2: announced AND has a game log (should be excluded from both lists).
    - P3: has a game log but no roster-sourced participation (late-add).

    Asserts totals and list membership/content (name + team) for each bucket.
    """
    competition_id = await _make_competition(db_session)
    team_entry_id = await _make_team_entry(db_session, competition_id)
    game_id = await _make_game(db_session, competition_id, "0012600001")

    sp1 = await _make_source_player(db_session, "P1", "Player One")
    sp2 = await _make_source_player(db_session, "P2", "Player Two")
    sp3 = await _make_source_player(db_session, "P3", "Player Three")

    await _announce(db_session, competition_id, team_entry_id, sp1)
    part2 = await _announce(db_session, competition_id, team_entry_id, sp2)
    await _game_log(
        db_session,
        competition_id,
        game_id,
        team_entry_id,
        sp2,
        participation_id=part2.id,
    )
    await _game_log(
        db_session, competition_id, game_id, team_entry_id, sp3, participation_id=None
    )
    await db_session.commit()

    report = await reconcile_competition(db_session, competition_id)

    assert report.competition_id == competition_id
    assert report.total_announced == 2  # P1, P2
    assert report.total_played == 2  # P2, P3
    assert report.announced_and_played == 1  # P2

    assert [e.source_player_id for e in report.announced_not_played] == [sp1.id]
    assert report.announced_not_played[0].name == "Player One"
    assert report.announced_not_played[0].team_name == "Cleveland Cavaliers"

    assert [e.source_player_id for e in report.played_not_announced] == [sp3.id]
    assert report.played_not_announced[0].name == "Player Three"
    assert report.played_not_announced[0].team_name == "Cleveland Cavaliers"


@pytest.mark.asyncio
async def test_null_participation_id_still_counts_as_played(
    db_session: AsyncSession,
) -> None:
    """A game log with a NULL participation_id still counts toward total_played.

    Regression guard for the ticket's explicit gotcha: joining game logs on
    participation_id (instead of (competition_id, source_player_id)) would
    silently drop pre-B1 rows that never got backfilled.
    """
    competition_id = await _make_competition(db_session)
    team_entry_id = await _make_team_entry(db_session, competition_id)
    game_id = await _make_game(db_session, competition_id, "0012600002")

    sp1 = await _make_source_player(db_session, "P1", "Player One")
    await _game_log(
        db_session, competition_id, game_id, team_entry_id, sp1, participation_id=None
    )
    await db_session.commit()

    report = await reconcile_competition(db_session, competition_id)

    assert report.total_played == 1
    assert report.total_announced == 0
    assert [e.source_player_id for e in report.played_not_announced] == [sp1.id]


@pytest.mark.asyncio
async def test_read_only_no_mutations_across_two_runs(db_session: AsyncSession) -> None:
    """Running reconcile_competition twice never mutates any row.

    Asserts row counts for participation, affiliation, and game-log tables are
    identical before and after two successive reconcile calls, and that the
    report content is identical across both calls.
    """
    competition_id = await _make_competition(db_session)
    team_entry_id = await _make_team_entry(db_session, competition_id)
    game_id = await _make_game(db_session, competition_id, "0012600003")

    sp1 = await _make_source_player(db_session, "P1", "Player One")
    sp2 = await _make_source_player(db_session, "P2", "Player Two")
    await _announce(db_session, competition_id, team_entry_id, sp1)
    await _game_log(
        db_session, competition_id, game_id, team_entry_id, sp2, participation_id=None
    )
    await db_session.commit()

    async def _counts() -> tuple[int, int, int]:
        part_count = (
            await db_session.execute(
                select(func.count()).select_from(SummerLeagueParticipation)
            )
        ).scalar_one()
        aff_count = (
            await db_session.execute(
                select(func.count()).select_from(PlayerAffiliation)
            )
        ).scalar_one()
        log_count = (
            await db_session.execute(
                select(func.count()).select_from(SummerLeaguePlayerGameLog)
            )
        ).scalar_one()
        return int(part_count), int(aff_count), int(log_count)

    before = await _counts()
    report1 = await reconcile_competition(db_session, competition_id)
    after_first = await _counts()
    report2 = await reconcile_competition(db_session, competition_id)
    after_second = await _counts()

    assert before == after_first == after_second

    assert report1.total_announced == report2.total_announced
    assert report1.total_played == report2.total_played
    assert report1.announced_and_played == report2.announced_and_played
    assert [e.source_player_id for e in report1.announced_not_played] == [
        e.source_player_id for e in report2.announced_not_played
    ]
    assert [e.source_player_id for e in report1.played_not_announced] == [
        e.source_player_id for e in report2.played_not_announced
    ]


@pytest.mark.asyncio
async def test_empty_competition(db_session: AsyncSession) -> None:
    """A competition with zero participations and zero game logs reconciles cleanly.

    Asserts all totals are zero and both flagged lists are empty.
    """
    competition_id = await _make_competition(db_session)

    report = await reconcile_competition(db_session, competition_id)

    assert report.total_announced == 0
    assert report.total_played == 0
    assert report.announced_and_played == 0
    assert report.announced_not_played == []
    assert report.played_not_announced == []


@pytest.mark.asyncio
async def test_unresolved_only_source_players(db_session: AsyncSession) -> None:
    """Unresolved source players (no canonical player_id) still reconcile.

    Both the announced and played entries have no ``player_id`` linkage yet;
    the reconcile service must fall back to the raw source-player name rather
    than erroring or omitting the entry.
    """
    competition_id = await _make_competition(db_session)
    team_entry_id = await _make_team_entry(db_session, competition_id)
    game_id = await _make_game(db_session, competition_id, "0012600004")

    sp1 = await _make_source_player(db_session, "P1", "Unresolved One")
    sp2 = await _make_source_player(db_session, "P2", "Unresolved Two")

    # sp1: announced only, unresolved (player_id=None).
    await _announce(db_session, competition_id, team_entry_id, sp1, player_id=None)
    # sp2: played only, unresolved (player_id=None).
    await _game_log(
        db_session,
        competition_id,
        game_id,
        team_entry_id,
        sp2,
        participation_id=None,
        player_id=None,
    )
    await db_session.commit()

    report = await reconcile_competition(db_session, competition_id)

    assert report.total_announced == 1
    assert report.total_played == 1
    assert report.announced_and_played == 0
    assert report.announced_not_played[0].name == "Unresolved One"
    assert report.played_not_announced[0].name == "Unresolved Two"


@pytest.mark.asyncio
async def test_resolved_player_prefers_canonical_display_name(
    db_session: AsyncSession,
) -> None:
    """A resolved participation prefers PlayerMaster.display_name over the raw name.

    Seeds a canonical ``PlayerMaster`` row and links it via participation and
    game-log ``player_id``; asserts the reconcile entry surfaces the canonical
    display name, not the raw NBA.com source name.
    """
    competition_id = await _make_competition(db_session)
    team_entry_id = await _make_team_entry(db_session, competition_id)

    player = PlayerMaster(
        first_name="Canonical",
        last_name="Player",
        display_name="Canonical Player",
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()

    sp1 = await _make_source_player(db_session, "P1", "cnncl plyr (raw)")
    await _announce(db_session, competition_id, team_entry_id, sp1, player_id=player.id)
    await db_session.commit()

    report = await reconcile_competition(db_session, competition_id)

    assert report.announced_not_played[0].name == "Canonical Player"

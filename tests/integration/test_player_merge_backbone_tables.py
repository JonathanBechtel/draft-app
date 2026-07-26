"""Merging a player who holds Summer League data must move it, not fail.

The failure this covers
-----------------------
``player_merge_service``'s child-table list was maintained by hand and drifted as the
``summer_league_*``, shot-event, play-by-play and participation tables added foreign keys to
``players_master``. Merging a player holding any of that data hard-failed on a RESTRICT FK,
in the middle of a time-sensitive identity cleanup.

``tests/unit/test_player_merge_fk_coverage.py`` proves every FK is *classified*. This proves
the classification actually works against a real database: each newly registered table is
seeded on the discard side, and the merge has to relocate it to the survivor.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueShotEvent,
)
from app.services.player_merge_service import merge_players
from tests.integration.conftest import make_player


_SEQ: dict[str, int] = {"n": 0}


def _uid() -> str:
    _SEQ["n"] += 1
    return f"mbk-{_SEQ['n']}"


async def _make_player(db: AsyncSession, first: str, last: str) -> PlayerMaster:
    """Persist a player and return it with an id."""
    player = make_player(first, last)
    db.add(player)
    await db.flush()
    return player


async def _seed_summer_league(
    db: AsyncSession, *, player: PlayerMaster
) -> dict[str, int]:
    """Give ``player`` a row in each Summer League table that references players_master."""
    assert player.id is not None

    comp = SummerLeagueCompetition(
        year=2026,
        league_id=f"15-{_uid()}",
        venue_slug="las_vegas",
        display_name="2026 Las Vegas Summer League",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None

    home = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=_uid(),
        raw_team_name=f"Home {_uid()}",
        raw_team_abbreviation="HOM",
        team_slug=f"home-{_uid()}",
    )
    away = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=_uid(),
        raw_team_name=f"Away {_uid()}",
        raw_team_abbreviation="AWY",
        team_slug=f"away-{_uid()}",
    )
    db.add(home)
    db.add(away)
    await db.flush()
    assert home.id is not None and away.id is not None

    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=_uid(),
        game_date=date(2026, 7, 12),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=101,
        away_score=99,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None

    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=_uid(),
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None

    db.add(
        SummerLeaguePlayerGameLog(
            game_id=game.id,
            competition_id=comp.id,
            team_entry_id=home.id,
            source_player_id=source_player.id,
            player_id=player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=player.display_name or "Player",
        )
    )
    db.add(
        SummerLeagueShotEvent(
            game_id=game.id,
            competition_id=comp.id,
            team_entry_id=home.id,
            source_player_id=source_player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            nba_stats_game_id=game.nba_stats_game_id,
            nba_stats_game_event_id=1,
            made=True,
            player_id=player.id,
        )
    )
    db.add(
        SummerLeaguePlayByPlayEvent(
            game_id=game.id,
            competition_id=comp.id,
            nba_stats_game_id=game.nba_stats_game_id,
            event_num=1,
            person1_id=player.id,
            person2_id=player.id,
            person3_id=player.id,
        )
    )
    await db.flush()

    # Tables without convenient models here are seeded directly; the point is the FK,
    # not the surrounding columns.
    await db.execute(
        text(
            "INSERT INTO summer_league_participation "
            "(competition_id, team_entry_id, source_player_id, player_id, stint_no, "
            " created_at, updated_at) "
            "VALUES (:c, :t, :sp, :p, 1, now(), now())"
        ),
        {"c": comp.id, "t": home.id, "sp": source_player.id, "p": player.id},
    )
    await db.execute(
        text(
            "INSERT INTO draft_results "
            "(draft_year, overall_pick, round, round_pick, raw_player_name, "
            " resolution_method, source, player_id, created_at, updated_at) "
            "VALUES (2026, :pick, 1, :pick, :name, 'exact', 'test', :p, now(), now())"
        ),
        {"pick": (player.id % 60) + 1, "name": player.display_name, "p": player.id},
    )
    await db.execute(
        text(
            "INSERT INTO player_affiliations "
            "(player_id, affiliation_type, source, recorded_at, created_at, updated_at) "
            "VALUES (:p, 'SUMMER_LEAGUE_ROSTER', 'test', now(), now(), now())"
        ),
        {"p": player.id},
    )
    await db.flush()

    return {"competition_id": comp.id, "game_id": game.id}


async def _count(db: AsyncSession, table: str, column: str, player_id: int) -> int:
    return int(
        (
            await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :pid"),
                {"pid": player_id},
            )
        ).scalar()
        or 0
    )


# Every Summer League / backbone column the merge must now relocate.
_REASSIGNED = (
    ("summer_league_player_game_logs", "player_id"),
    ("summer_league_shot_events", "player_id"),
    ("summer_league_play_by_play_events", "person1_id"),
    ("summer_league_play_by_play_events", "person2_id"),
    ("summer_league_play_by_play_events", "person3_id"),
    ("summer_league_source_players", "canonical_player_id"),
    ("summer_league_participation", "player_id"),
    ("draft_results", "player_id"),
    ("player_affiliations", "player_id"),
)


@pytest.mark.asyncio
async def test_merge_relocates_summer_league_data_instead_of_failing(
    db_session: AsyncSession,
) -> None:
    """The exact scenario that used to raise on a RESTRICT foreign key.

    A duplicate holding Summer League game logs, shot events, play-by-play, participation,
    a draft result and an affiliation is merged away; every row must end up on the survivor
    and the duplicate must be gone.
    """
    keep = await _make_player(db_session, "Merge", "Survivor")
    discard = await _make_player(db_session, "Merge", "Duplicate")
    assert keep.id is not None and discard.id is not None

    await _seed_summer_league(db_session, player=discard)

    before = {ref: await _count(db_session, *ref, discard.id) for ref in _REASSIGNED}
    assert all(n > 0 for n in before.values()), f"seeding incomplete: {before}"

    await merge_players(db_session, keep_id=keep.id, discard_id=discard.id)

    for table, column in _REASSIGNED:
        remaining = await _count(db_session, table, column, discard.id)
        moved = await _count(db_session, table, column, keep.id)
        assert remaining == 0, f"{table}.{column} still points at the discarded player"
        assert moved >= before[(table, column)], (
            f"{table}.{column} lost rows: expected at least "
            f"{before[(table, column)]} on the survivor, found {moved}"
        )

    survivors = int(
        (
            await db_session.execute(
                text("SELECT count(*) FROM players_master WHERE id = :pid"),
                {"pid": discard.id},
            )
        ).scalar()
        or 0
    )
    assert survivors == 0, "the discarded player row should be gone"


@pytest.mark.asyncio
async def test_merge_still_works_when_the_survivor_also_holds_data(
    db_session: AsyncSession,
) -> None:
    """Both sides holding Summer League data must merge, not collide.

    This is the case that cannot happen in today's production data but is exactly what a
    future duplicate would look like once ingestion resolves logs onto it.
    """
    keep = await _make_player(db_session, "Bothsides", "Keeper")
    discard = await _make_player(db_session, "Bothsides", "Discard")
    assert keep.id is not None and discard.id is not None

    await _seed_summer_league(db_session, player=keep)
    await _seed_summer_league(db_session, player=discard)

    keep_before = await _count(
        db_session, "summer_league_player_game_logs", "player_id", keep.id
    )
    discard_before = await _count(
        db_session, "summer_league_player_game_logs", "player_id", discard.id
    )
    assert keep_before and discard_before

    await merge_players(db_session, keep_id=keep.id, discard_id=discard.id)

    assert (
        await _count(
            db_session, "summer_league_player_game_logs", "player_id", discard.id
        )
        == 0
    )
    assert (
        await _count(db_session, "summer_league_player_game_logs", "player_id", keep.id)
        == keep_before + discard_before
    ), "both sides' game logs should survive on the keeper"

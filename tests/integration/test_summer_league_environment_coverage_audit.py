"""Integration tests for the Competition Context Phase 0 coverage audit (#616).

Seeds a representative multi-year/multi-competition Summer League fixture into a
disposable database and asserts the read-only audit's coverage inventory:
two competitions with unequal coverage, one repeat canonical player across
venues, one unresolved player, a DNP shell, and non-final games.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from scripts.audit_summer_league_environment_coverage import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    collect_coverage,
)
from tests.integration.conftest import make_player

_SEQ = {"n": 0}


def _next() -> int:
    """Monotonic counter for unique source IDs across seed calls."""
    _SEQ["n"] += 1
    return _SEQ["n"]


async def _seed_competition(
    db: AsyncSession, *, year: int, league_id: str, venue_slug: str
) -> tuple[SummerLeagueEdition, SummerLeagueTeamEntry, SummerLeagueTeamEntry]:
    """Seed one competition with two team entries."""
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    teams = []
    for side in ("home", "away"):
        n = _next()
        team = SummerLeagueTeamEntry(
            competition_id=comp.id,
            nba_stats_team_id=f"team-{n}",
            raw_team_name=f"Team {side} {n}",
            raw_team_abbreviation=side[:3].upper(),
            team_slug=f"team-{n}",
        )
        db.add(team)
        teams.append(team)
    await db.flush()
    return comp, teams[0], teams[1]


async def _seed_final_game(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    box_complete: bool = True,
    status_text: str | None = "Final",
) -> SummerLeagueGame:
    """Seed a final game with two team-box rows (complete unless told otherwise)."""
    n = _next()
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"game-{n}",
        game_date=date(comp.year, 7, 3),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=90,
        away_score=85,
        status=SummerLeagueGameStatus.FINAL,
        status_text=status_text,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    for idx, team in enumerate((home, away)):
        # Second team is left incomplete (null fga) when box_complete is False.
        incomplete = (not box_complete) and idx == 1
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=team.id,
                minutes=200,
                pts=90 - idx * 5,
                fgm=35,
                fga=None if incomplete else 78,
                fg3m=10,
                fg3a=28,
                ftm=12,
                fta=16,
                oreb=10,
                dreb=30,
                reb=40,
                ast=20,
                stl=6,
                blk=3,
                tov=13,
                pf=18,
            )
        )
    await db.flush()
    return game


async def _seed_scheduled_game(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
) -> None:
    """Seed a non-final (scheduled) game that must never contribute metrics."""
    n = _next()
    db.add(
        SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"game-{n}",
            game_date=date(comp.year, 7, 5),
            home_team_entry_id=home.id,
            away_team_entry_id=away.id,
            status=SummerLeagueGameStatus.SCHEDULED,
            status_text=None,
        )
    )
    await db.flush()


async def _seed_source_player(
    db: AsyncSession, *, name: str, canonical_player_id: int | None
) -> SummerLeagueSourcePlayer:
    """Seed one NBA-source player identity, resolved or not."""
    n = _next()
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"person-{n}",
        raw_player_name=name,
        normalized_name=name.lower(),
        canonical_player_id=canonical_player_id,
    )
    db.add(sp)
    await db.flush()
    assert sp.id is not None
    return sp


async def _seed_player_log(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    source: SummerLeagueSourcePlayer,
    canonical_player_id: int | None,
    minutes_seconds: int | None,
) -> None:
    """Seed one player-game box line (DNP when minutes_seconds is None/0)."""
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=source.id,
            player_id=canonical_player_id,
            nba_stats_person_id=source.nba_stats_person_id,
            raw_player_name=source.raw_player_name,
            minutes_seconds=minutes_seconds,
            pts=12,
            fgm=5,
            fga=10,
        )
    )
    await db.flush()


async def _seed_shot(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    source: SummerLeagueSourcePlayer,
) -> None:
    """Seed one shot event so a game counts as shot-covered."""
    n = _next()
    db.add(
        SummerLeagueShotEvent(
            game_id=game.id,
            competition_id=comp.id,
            team_entry_id=team.id,
            source_player_id=source.id,
            nba_stats_person_id=source.nba_stats_person_id,
            nba_stats_game_id=game.nba_stats_game_id,
            nba_stats_game_event_id=n,
            shot_zone_basic="Restricted Area",
            made=True,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_audit_reports_unequal_coverage_and_dedups_season_players(
    db_session: AsyncSession,
) -> None:
    """Two 2025 competitions with unequal coverage, a repeat and unresolved player.

    Vegas: 2 final games, box + shot complete, played by a repeat canonical
    player, a second canonical player, an unresolved player, and a DNP shell.
    California Classic: 1 final game (box complete, no shots) + 1 scheduled game,
    played by the repeat canonical player only.
    """
    db = db_session

    # Canonical players with varying attribute completeness.
    repeat = make_player("Repeat", "Player", school="Duke")  # draft/school known
    repeat.birthdate = date(2003, 1, 1)
    repeat.position = "G"
    repeat.birth_country = "USA"
    second = make_player("Second", "Player")  # draft_year known, no school/country
    second.birthdate = None
    second.position = None
    second.birth_country = None
    second.school = None
    db.add(repeat)
    db.add(second)
    await db.flush()
    assert repeat.id is not None and second.id is not None

    # --- Vegas competition (full coverage) ---
    vegas, v_home, v_away = await _seed_competition(
        db, year=2025, league_id="15", venue_slug="las-vegas"
    )
    v_g1 = await _seed_final_game(db, comp=vegas, home=v_home, away=v_away)
    v_g2 = await _seed_final_game(db, comp=vegas, home=v_home, away=v_away)

    sp_repeat_v = await _seed_source_player(
        db, name="Repeat Player", canonical_player_id=repeat.id
    )
    sp_second = await _seed_source_player(
        db, name="Second Player", canonical_player_id=second.id
    )
    sp_unresolved = await _seed_source_player(
        db, name="Mystery Guy", canonical_player_id=None
    )
    sp_dnp = await _seed_source_player(
        db, name="Bench Warmer", canonical_player_id=second.id
    )

    for game in (v_g1, v_g2):
        await _seed_player_log(
            db, comp=vegas, game=game, team=v_home, source=sp_repeat_v,
            canonical_player_id=repeat.id, minutes_seconds=1200,
        )
        await _seed_player_log(
            db, comp=vegas, game=game, team=v_home, source=sp_second,
            canonical_player_id=second.id, minutes_seconds=900,
        )
        await _seed_player_log(
            db, comp=vegas, game=game, team=v_away, source=sp_unresolved,
            canonical_player_id=None, minutes_seconds=800,
        )
        # DNP shell (zero minutes) — must not count as an appearance.
        await _seed_player_log(
            db, comp=vegas, game=game, team=v_home, source=sp_dnp,
            canonical_player_id=second.id, minutes_seconds=0,
        )
        await _seed_shot(db, comp=vegas, game=game, team=v_home, source=sp_repeat_v)

    # --- California Classic (box only, no shots, one scheduled game) ---
    cc, c_home, c_away = await _seed_competition(
        db, year=2025, league_id="13", venue_slug="california-classic"
    )
    c_g1 = await _seed_final_game(db, comp=cc, home=c_home, away=c_away)
    await _seed_scheduled_game(db, comp=cc, home=c_home, away=c_away)

    sp_repeat_c = await _seed_source_player(
        db, name="Repeat Player", canonical_player_id=repeat.id
    )
    await _seed_player_log(
        db, comp=cc, game=c_g1, team=c_home, source=sp_repeat_c,
        canonical_player_id=repeat.id, minutes_seconds=1000,
    )
    await db.commit()

    report = await collect_coverage(db)

    comps = {c.venue_slug: c for c in report.competitions}
    seasons = {s.year: s for s in report.seasons}
    assert set(comps) == {"las-vegas", "california-classic"}
    assert set(seasons) == {2025}

    vegas_rec = comps["las-vegas"]
    assert vegas_rec.final_games == 2
    assert vegas_rec.box_complete_games == 2
    assert vegas_rec.shot_covered_games == 2
    assert vegas_rec.pbp_covered_games == 0
    # repeat + second resolved; unresolved counted separately; DNP excluded.
    assert vegas_rec.appeared_canonical == 2
    assert vegas_rec.appeared_unresolved == 1
    # 3 appearances per game x 2 games (DNP excluded).
    assert vegas_rec.appeared_player_games == 6
    assert vegas_rec.metric_certifiability()["pace_per_48"] == COVERAGE_COMPLETE
    assert vegas_rec.metric_certifiability()["rim_fg_pct"] == COVERAGE_COMPLETE

    cc_rec = comps["california-classic"]
    assert cc_rec.final_games == 1
    assert cc_rec.box_complete_games == 1
    assert cc_rec.shot_covered_games == 0
    assert cc_rec.status_counts["scheduled"] == 1
    assert cc_rec.appeared_canonical == 1  # only the repeat player
    assert cc_rec.metric_certifiability()["pace_per_48"] == COVERAGE_COMPLETE
    assert cc_rec.metric_certifiability()["rim_fg_pct"] == COVERAGE_UNAVAILABLE

    season = seasons[2025]
    assert season.included_competitions == 2
    assert season.final_games == 3
    assert season.box_complete_games == 3
    assert season.shot_covered_games == 2
    # Repeat player dedups across venues: distinct people = repeat + second.
    assert season.appeared_canonical == 2
    assert season.appeared_unresolved == 1
    # Additive across venues: 6 (Vegas) + 1 (CC).
    assert season.appeared_player_games == 7
    # Box metrics certify (3/3); shot metrics only partial (2/3).
    assert season.metric_certifiability()["pace_per_48"] == COVERAGE_COMPLETE
    assert season.metric_certifiability()["rim_attempt_share"] == COVERAGE_PARTIAL

    # Attribute coverage over the 2 distinct resolved season players.
    assert season.resolved_appeared == 2
    assert season.attributes["draft"].known == 2  # both have draft_year (make_player)
    assert season.attributes["draft"].total == 2
    # Only the repeat player has birthdate/position/country.
    assert season.attributes["age"].known == 1
    assert season.attributes["age"].unknown == 1
    assert season.attributes["position"].known == 1
    assert season.attributes["origin"].known == 1

@pytest.mark.asyncio
async def test_audit_is_read_only(db_session: AsyncSession) -> None:
    """The audit issues no writes: row counts are unchanged after collection."""
    db = db_session
    comp, home, away = await _seed_competition(
        db, year=2024, league_id="15", venue_slug="las-vegas"
    )
    await _seed_final_game(db, comp=comp, home=home, away=away)
    await db.commit()

    before = (
        await db.execute(_count_sql("summer_league_games"))
    ).scalar_one()
    await collect_coverage(db)
    after = (
        await db.execute(_count_sql("summer_league_games"))
    ).scalar_one()
    assert before == after == 1


def _count_sql(table: str):
    """Build a COUNT(*) statement for a table."""
    from sqlalchemy import text

    return text(f"SELECT COUNT(*) FROM {table}")

"""Integration coverage for franchise/team_program resolution at ingest time (#796).

Verifies the ingest write-path gap ticket #796 closes: normalizing a
competition against a *pre-populated* org model (organizations/team_programs,
created here via T3's population script exactly as an operator would run it)
must resolve ``nba_team_id`` and ``team_program_id`` on newly created
``summer_league_team_entries`` rows -- not leave them for a later backfill
sweep. Also proves a non-NBA/select squad with no franchise mapping lands
NULL on both, by design, and that ``scripts/backfill_sl_team_entry_team_program.py
--dry-run`` reports zero eligible rows once ingest has already set the targets
itself.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.summer_league import SummerLeagueEdition, SummerLeagueTeamEntry
from app.services.sources.summer_league.audit import audit_summer_league_raw
from app.services.sources.summer_league.normalization import (
    normalize_competition_games,
)
from app.services.sources.summer_league.roster_ingest import (
    CompetitionKey,
    load_roster_snapshot,
)
from app.services.sources.summer_league.roster_parse import RosterEntry
from scripts.backfill_sl_team_entry_team_program import (
    run_backfill as run_sl_team_entry_backfill,
)
from scripts.populate_org_model_from_nba_teams import run_population

YEAR = 2026
LEAGUE_ID = "15"
GAME_ID = "1526400001"
ORL_STATS_TEAM_ID = 1610612753  # a real NBA franchise (Orlando Magic)
NON_FRANCHISE_STATS_TEAM_ID = 555555555  # no franchise mapping exists for this id


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def _write_fixture(raw_root: Path) -> None:
    """Write a minimal raw fixture: one game, a known franchise vs. an unmapped squad."""
    run_dir = raw_root / str(YEAR) / LEAGUE_ID
    game_dir = run_dir / "games" / GAME_ID
    game_dir.mkdir(parents=True)
    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": YEAR,
                "league_id": LEAGUE_ID,
                "venue": "las_vegas",
                "team_gamelog_rows": 2,
                "player_gamelog_rows": 0,
                "game_ids": [GAME_ID],
                "game_count": 1,
                "errors": [],
            }
        )
    )
    run_dir.joinpath("leaguegamelog_team.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        [
                            "TEAM_ID",
                            "TEAM_ABBREVIATION",
                            "TEAM_NAME",
                            "GAME_ID",
                            "GAME_DATE",
                            "MATCHUP",
                            "PTS",
                        ],
                        [
                            [
                                ORL_STATS_TEAM_ID,
                                "ORL",
                                "Orlando Magic",
                                GAME_ID,
                                "2026-07-12",
                                "ORL vs. XXX",
                                100,
                            ],
                            [
                                NON_FRANCHISE_STATS_TEAM_ID,
                                "XXX",
                                "Non Franchise Squad",
                                GAME_ID,
                                "2026-07-12",
                                "XXX @ ORL",
                                90,
                            ],
                        ],
                    )
                ]
            }
        )
    )
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps({"resultSets": [_result_set("LeagueGameLog", ["PLAYER_ID"], [])]})
    )
    game_dir.joinpath("boxscoretraditionalv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("PlayerStats", [], []),
                    _result_set(
                        "TeamStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "TEAM_NAME",
                            "TEAM_ABBREVIATION",
                            "MIN",
                            "FGM",
                            "FGA",
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                GAME_ID,
                                ORL_STATS_TEAM_ID,
                                "Magic",
                                "ORL",
                                "200:00",
                                36,
                                76,
                                100,
                                10,
                            ],
                            [
                                GAME_ID,
                                NON_FRANCHISE_STATS_TEAM_ID,
                                "Non Franchise Squad",
                                "XXX",
                                "200:00",
                                30,
                                80,
                                90,
                                -10,
                            ],
                        ],
                    ),
                ]
            }
        )
    )
    game_dir.joinpath("boxscoreadvancedv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("PlayerStats", [], []),
                    _result_set("TeamStats", [], []),
                ]
            }
        )
    )
    game_dir.joinpath("boxscorescoringv2.json").write_text(
        json.dumps({"resultSets": []})
    )
    game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))
    game_dir.joinpath("shotchartdetail.json").write_text(json.dumps({"resultSets": []}))


async def _seed_org_model_for_orlando_magic(db: AsyncSession) -> None:
    """Seed one ``nba_teams`` row and run T3's population, as an operator would."""
    team = NbaTeam(name="Orlando Magic", abbreviation="ORL", slug="magic")
    db.add(team)
    await db.commit()

    report = await run_population(db)
    assert report.failed == 0


async def _team_entry(
    db: AsyncSession, *, competition_id: int, nba_stats_team_id: int
) -> SummerLeagueTeamEntry:
    result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueTeamEntry.nba_stats_team_id == str(nba_stats_team_id),  # type: ignore[arg-type]
        )
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_normalize_competition_games_resolves_targets_for_a_known_franchise(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A fresh ingest against a pre-populated org model sets both targets."""
    await _seed_org_model_for_orlando_magic(db_session)
    _write_fixture(tmp_path)

    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=YEAR, league_id=LEAGUE_ID
    )
    report = await normalize_competition_games(
        db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
    )
    await db_session.flush()

    orl_entry = await _team_entry(
        db_session,
        competition_id=report.competition_id,
        nba_stats_team_id=ORL_STATS_TEAM_ID,
    )

    assert orl_entry.nba_team_id is not None
    assert orl_entry.team_program_id is not None
    assert report.team_entries_created_unresolved == 1  # only the non-franchise squad


@pytest.mark.asyncio
async def test_normalize_competition_games_leaves_non_franchise_squad_null_on_both(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A squad with no NBA franchise mapping lands NULL on both, not invented."""
    await _seed_org_model_for_orlando_magic(db_session)
    _write_fixture(tmp_path)

    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=YEAR, league_id=LEAGUE_ID
    )
    report = await normalize_competition_games(
        db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
    )
    await db_session.flush()

    non_franchise_entry = await _team_entry(
        db_session,
        competition_id=report.competition_id,
        nba_stats_team_id=NON_FRANCHISE_STATS_TEAM_ID,
    )

    assert non_franchise_entry.nba_team_id is None
    assert non_franchise_entry.team_program_id is None


@pytest.mark.asyncio
async def test_backfill_dry_run_reports_zero_eligible_after_a_fresh_ingest(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The operator backfill has nothing left once ingest sets targets itself.

    Direct evidence for the DoD bullet: re-running
    ``scripts/backfill_sl_team_entry_team_program.py --dry-run`` after an
    ingest reports zero eligible rows.
    """
    await _seed_org_model_for_orlando_magic(db_session)
    _write_fixture(tmp_path)

    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=YEAR, league_id=LEAGUE_ID
    )
    await normalize_competition_games(
        db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    backfill_report = await run_sl_team_entry_backfill(db_session, dry_run=True)

    assert backfill_report.eligible == 0


def _roster_entry(
    *, person_id: str, team_id: str, name: str = "Test Player"
) -> RosterEntry:
    return RosterEntry(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        team_id=team_id,
        jersey="0",
        position="G",
        height="6-3",
        weight="185",
        birth_date=None,
        school=None,
        how_acquired=None,
        league_id=LEAGUE_ID,
    )


@pytest.mark.asyncio
async def test_load_roster_snapshot_resolves_targets_at_the_second_creation_site(
    db_session: AsyncSession,
) -> None:
    """The roster-ingest team-entry creation site also resolves both targets.

    Ticket #796 names two team-entry creation sites --
    ``normalization.py``'s box-score path (covered above) and
    ``roster_ingest.py``'s roster-pull path. This proves the second site
    independently.
    """
    await _seed_org_model_for_orlando_magic(db_session)
    competition = CompetitionKey(year=YEAR, league_id=LEAGUE_ID, venue_slug="las_vegas")
    entries = [
        _roster_entry(person_id="900001", team_id=str(ORL_STATS_TEAM_ID)),
        _roster_entry(
            person_id="900002",
            team_id=str(NON_FRANCHISE_STATS_TEAM_ID),
            name="Other Player",
        ),
    ]

    report = await load_roster_snapshot(
        db_session, competition, entries, recorded_at=datetime(2026, 7, 1)
    )
    await db_session.flush()

    edition_result = await db_session.execute(
        select(SummerLeagueEdition).where(
            SummerLeagueEdition.year == YEAR,  # type: ignore[arg-type]
            SummerLeagueEdition.league_id == LEAGUE_ID,  # type: ignore[arg-type]
        )
    )
    competition_row = edition_result.scalar_one()
    assert competition_row.id is not None

    orl_entry = await _team_entry(
        db_session,
        competition_id=competition_row.id,
        nba_stats_team_id=ORL_STATS_TEAM_ID,
    )
    non_franchise_entry = await _team_entry(
        db_session,
        competition_id=competition_row.id,
        nba_stats_team_id=NON_FRANCHISE_STATS_TEAM_ID,
    )

    assert orl_entry.nba_team_id is not None
    assert orl_entry.team_program_id is not None
    assert non_franchise_entry.nba_team_id is None
    assert non_franchise_entry.team_program_id is None
    assert report.team_entries_created_unresolved == 1

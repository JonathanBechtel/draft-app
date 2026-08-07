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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.summer_league import SummerLeagueEdition
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
    """Seed two ``nba_teams`` rows and run T3's population, as an operator would.

    The Lakers row is a **decoy**, not scenery. With a single franchise seeded,
    ``team_program_id is not None`` passes for any implementation that reaches
    *some* program -- "the first ``team_programs`` row", say, or a re-derived
    lookup that ignores the team entry it was handed. Seeding a second franchise
    means only a resolver that maps Orlando's provider id to Orlando's program
    can satisfy the equality assertions below.
    """
    db.add(NbaTeam(name="Orlando Magic", abbreviation="ORL", slug="magic"))
    db.add(NbaTeam(name="Los Angeles Lakers", abbreviation="LAL", slug="lakers"))
    await db.commit()

    report = await run_population(db)
    assert report.failed == 0


async def _franchise_targets(db: AsyncSession, *, slug: str) -> tuple[int, int]:
    """Return ``(nba_teams.id, team_programs.id)`` for one seeded franchise.

    Read with raw SQL so the expectation is the database's own view of what T3
    created, never an ORM object this test is also asserting against.
    """
    row = (
        await db.execute(
            text(
                "SELECT t.id, p.id FROM nba_teams t "
                "JOIN organizations o ON o.slug = 'nba-' || t.slug "
                "JOIN team_programs p ON p.organization_id = o.id "
                "WHERE t.slug = :slug"
            ),
            {"slug": slug},
        )
    ).one()
    return (row[0], row[1])


async def _team_entry_targets_raw(
    db: AsyncSession, *, competition_id: int, nba_stats_team_id: int
) -> tuple[int | None, int | None]:
    """Read a team entry's two target columns straight from Postgres.

    The integration ``db_session`` fixture sets ``expire_on_commit=False``, so
    an ORM re-read in the same session can be answered from the identity map --
    the value the test just wrote, not the row. SQLModel also discards unknown
    constructor kwargs silently, so a round-trip through the ORM would still
    "pass" if a column had been dropped. Naming the columns in SQL removes both
    failure modes at once.
    """
    db.expunge_all()
    row = (
        await db.execute(
            text(
                "SELECT nba_team_id, team_program_id FROM summer_league_team_entries "
                "WHERE competition_id = :competition_id "
                "AND nba_stats_team_id = :nba_stats_team_id"
            ),
            {
                "competition_id": competition_id,
                "nba_stats_team_id": str(nba_stats_team_id),
            },
        )
    ).one()
    return (row[0], row[1])


@pytest.mark.asyncio
async def test_normalize_competition_games_resolves_targets_for_a_known_franchise(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A fresh ingest against a pre-populated org model sets both targets.

    Asserts the *specific* Orlando targets, not merely non-NULL: a decoy
    franchise (Lakers) is seeded alongside, so resolving to "some program"
    fails here.
    """
    await _seed_org_model_for_orlando_magic(db_session)
    orl_team_id, orl_program_id = await _franchise_targets(db_session, slug="magic")
    _write_fixture(tmp_path)

    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=YEAR, league_id=LEAGUE_ID
    )
    report = await normalize_competition_games(
        db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
    )
    await db_session.flush()

    nba_team_id, team_program_id = await _team_entry_targets_raw(
        db_session,
        competition_id=report.competition_id,
        nba_stats_team_id=ORL_STATS_TEAM_ID,
    )

    assert nba_team_id == orl_team_id
    assert team_program_id == orl_program_id
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

    targets = await _team_entry_targets_raw(
        db_session,
        competition_id=report.competition_id,
        nba_stats_team_id=NON_FRANCHISE_STATS_TEAM_ID,
    )

    assert targets == (None, None)


@pytest.mark.asyncio
async def test_backfill_dry_run_reports_zero_eligible_after_a_fresh_ingest(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The operator backfill has nothing left once ingest sets targets itself.

    Direct evidence for the DoD bullet: re-running
    ``scripts/backfill_sl_team_entry_team_program.py --dry-run`` after an
    ingest reports zero eligible rows.

    ``eligible == 0`` alone does **not** prove that. ``eligible`` counts rows
    with ``nba_team_id IS NOT NULL AND team_program_id IS NULL``, and before
    #796 the ingest set *neither* column -- so a pre-#796 ingest also produced
    ``eligible == 0``, for the opposite reason (nothing for the join to key
    on). The load-bearing assertion is therefore ``left_null``, which counts
    rows with a NULL ``nba_team_id``: exactly 1 after #796 (the non-franchise
    squad, correctly unresolved), but 2 before it (both squads). Asserting
    both pins the distinction the ticket is about -- the backfill is idle
    because ingest *resolved* the rows, not because it never reached them.
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
    assert backfill_report.left_null == 1


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
    independently, against the *specific* Orlando targets rather than merely
    non-NULL ones -- a decoy franchise is seeded alongside.
    """
    await _seed_org_model_for_orlando_magic(db_session)
    orl_team_id, orl_program_id = await _franchise_targets(db_session, slug="magic")
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

    competition_id = competition_row.id
    orl_targets = await _team_entry_targets_raw(
        db_session,
        competition_id=competition_id,
        nba_stats_team_id=ORL_STATS_TEAM_ID,
    )
    non_franchise_targets = await _team_entry_targets_raw(
        db_session,
        competition_id=competition_id,
        nba_stats_team_id=NON_FRANCHISE_STATS_TEAM_ID,
    )

    assert orl_targets == (orl_team_id, orl_program_id)
    assert non_franchise_targets == (None, None)
    assert report.team_entries_created_unresolved == 1

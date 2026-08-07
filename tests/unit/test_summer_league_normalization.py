"""Unit tests for Summer League competition/team/game normalization helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueSourceDocument,
    SummerLeagueRawFileStatus,
    SummerLeagueIngestionRun,
    SummerLeagueRawRunStatus,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.sources.summer_league import normalization as service
from app.services.sources.summer_league.normalization import (
    ParsedTeamBoxRow,
    ParsedTeamGamelogRow,
    _team_box_row_from_gamelog,
    normalize_competition_games,
    parse_minutes_to_int,
    parse_player_gamelog_box_rows,
    parse_team_box_rows,
    parse_team_gamelog,
    resolve_game_status,
    team_slug,
)


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def test_parse_minutes_to_int_handles_nba_values() -> None:
    """Minute values parse from numeric and NBA clock strings."""
    assert parse_minutes_to_int("200:00") == 200
    assert parse_minutes_to_int("24:28") == 24
    assert parse_minutes_to_int(213) == 213
    assert parse_minutes_to_int("") is None


def test_team_slug_uses_source_name_or_abbreviation() -> None:
    """Team slugs are deterministic from source names."""
    assert team_slug("Los Angeles Lakers", "LAL") == "los-angeles-lakers"
    assert team_slug("", "NYK") == "nyk"


@pytest.mark.parametrize(
    "current_status, raw_run_complete, expected",
    [
        # No scoreboard row has ever tracked this game (brand-new row, the
        # schema default) and the audited slice isn't fully captured yet --
        # no evidence either way, stays Unknown.
        (SummerLeagueGameStatus.UNKNOWN, False, SummerLeagueGameStatus.UNKNOWN),
        # No scoreboard tracking, but the whole raw run is COMPLETE -- the
        # historic full-backfill path (matches the normalizer's original,
        # pre-#530 behavior for standalone historic ingests).
        (SummerLeagueGameStatus.UNKNOWN, True, SummerLeagueGameStatus.FINAL),
        # Scoreboard says Scheduled -- box data parsing (regardless of the
        # audited run's completeness) can never promote this on its own.
        (SummerLeagueGameStatus.SCHEDULED, False, SummerLeagueGameStatus.SCHEDULED),
        (SummerLeagueGameStatus.SCHEDULED, True, SummerLeagueGameStatus.SCHEDULED),
        # Scoreboard says In-Progress -- same guarantee: a targeted live raw
        # refresh's partial mid-game snapshot must never finalize the game.
        (SummerLeagueGameStatus.IN_PROGRESS, False, SummerLeagueGameStatus.IN_PROGRESS),
        (SummerLeagueGameStatus.IN_PROGRESS, True, SummerLeagueGameStatus.IN_PROGRESS),
        # Once Final, monotonic: a later partial/stale call never regresses it.
        (SummerLeagueGameStatus.FINAL, False, SummerLeagueGameStatus.FINAL),
        (SummerLeagueGameStatus.FINAL, True, SummerLeagueGameStatus.FINAL),
        # Fix #4: POSTPONED/CANCELED are terminal like FINAL -- a game that will
        # never tip must never get promoted to FINAL just because the audited
        # raw run for its year/league happens to be COMPLETE (evidence the
        # *other* games in that slice finished, not this one), nor regressed.
        (SummerLeagueGameStatus.POSTPONED, False, SummerLeagueGameStatus.POSTPONED),
        (SummerLeagueGameStatus.POSTPONED, True, SummerLeagueGameStatus.POSTPONED),
        (SummerLeagueGameStatus.CANCELED, False, SummerLeagueGameStatus.CANCELED),
        (SummerLeagueGameStatus.CANCELED, True, SummerLeagueGameStatus.CANCELED),
    ],
)
def test_resolve_game_status_table(
    current_status: SummerLeagueGameStatus,
    raw_run_complete: bool,
    expected: SummerLeagueGameStatus,
) -> None:
    """Pure status table (#530): missing evidence never promotes; Final is monotonic."""
    assert (
        resolve_game_status(
            current_status=current_status, raw_run_complete=raw_run_complete
        )
        == expected
    )


def test_parse_player_gamelog_box_rows_builds_traditional_line(
    tmp_path: Path,
) -> None:
    """Season LeagueGameLog yields full traditional box rows (pre-2017 fallback).

    Older Summer League years have no per-game boxscore data, so player game logs
    are rebuilt from the season log. MIN is whole minutes (→ seconds), advanced
    fields stay ``None``, and identity IDs are stringified.
    """
    path = tmp_path / "leaguegamelog_player.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        [
                            "PLAYER_ID",
                            "PLAYER_NAME",
                            "TEAM_ID",
                            "GAME_ID",
                            "MIN",
                            "FGM",
                            "FGA",
                            "FG3M",
                            "FTM",
                            "FTA",
                            "REB",
                            "AST",
                            "TOV",
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                203503,
                                "Tony Snell",
                                1610612741,
                                "1521300053",
                                34,
                                6,
                                13,
                                5,
                                3,
                                3,
                                7,
                                3,
                                2,
                                20,
                                7,
                            ]
                        ],
                    )
                ]
            }
        )
    )

    rows = parse_player_gamelog_box_rows(path)

    assert len(rows) == 1
    row = rows[0]
    assert row.game_id == "1521300053"
    assert row.nba_stats_person_id == "203503"
    assert row.nba_stats_team_id == "1610612741"
    assert row.raw_player_name == "Tony Snell"
    assert row.minutes_seconds == 34 * 60  # whole minutes → seconds
    assert row.pts == 20
    assert row.fgm == 6 and row.fga == 13 and row.fg3m == 5
    assert row.plus_minus == 7
    # Advanced/scoring fields are absent from the season log.
    assert row.usg_pct is None and row.off_rating is None


def test_parse_player_gamelog_box_rows_skips_rows_missing_identity(
    tmp_path: Path,
) -> None:
    """Rows lacking GAME_ID / PLAYER_ID / TEAM_ID are dropped (unplottable)."""
    path = tmp_path / "leaguegamelog_player.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        ["PLAYER_ID", "TEAM_ID", "GAME_ID", "PTS"],
                        [
                            [203503, 1610612741, None, 20],  # no game id
                            [None, 1610612741, "1521300053", 5],  # no player id
                        ],
                    )
                ]
            }
        )
    )
    assert parse_player_gamelog_box_rows(path) == []


def test_parse_team_gamelog_extracts_source_team_and_game_fields(
    tmp_path: Path,
) -> None:
    """Team gamelog parser extracts stable IDs, dates, matchup, and points."""
    path = tmp_path / "leaguegamelog_team.json"
    path.write_text(
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
                                1610612748,
                                "MIA",
                                "Miami Heat",
                                "1522400076",
                                "2024-07-22",
                                "MIA vs. MEM",
                                120,
                            ]
                        ],
                    )
                ]
            }
        )
    )

    rows = parse_team_gamelog(path)

    assert len(rows) == 1
    assert rows[0].game_id == "1522400076"
    assert rows[0].nba_stats_team_id == "1610612748"
    assert rows[0].game_date is not None
    assert rows[0].pts == 120


def test_parse_team_box_rows_merges_traditional_and_advanced(tmp_path: Path) -> None:
    """Team box parser merges traditional and advanced TeamStats rows."""
    game_dir = tmp_path / "games" / "1522400001"
    game_dir.mkdir(parents=True)
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
                                "1522400001",
                                1610612753,
                                "Magic",
                                "ORL",
                                "200:00",
                                36,
                                76,
                                106,
                                4,
                            ]
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
                    _result_set(
                        "TeamStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "TEAM_NAME",
                            "TEAM_ABBREVIATION",
                            "OFF_RATING",
                            "DEF_RATING",
                            "PACE",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                "Magic",
                                "ORL",
                                115.2,
                                85.9,
                                110.84,
                            ]
                        ],
                    ),
                ]
            }
        )
    )

    rows = parse_team_box_rows(tmp_path)

    assert len(rows) == 1
    assert rows[0].nba_stats_team_id == "1610612753"
    assert rows[0].minutes == 200
    assert rows[0].pts == 106
    assert rows[0].off_rating == 115.2
    assert rows[0].pace == 110.84


def test_team_box_row_from_gamelog_preserves_available_source_fields() -> None:
    """Gamelog fallback rows keep IDs and points while leaving box-only stats empty."""
    row = ParsedTeamGamelogRow(
        game_id="1520700003",
        game_date=None,
        nba_stats_team_id="45",
        raw_team_name="Team China Basketball",
        raw_team_abbreviation="CHN",
        matchup="CHN @ MEM",
        pts=77,
    )

    box_row = _team_box_row_from_gamelog(row)

    assert box_row.game_id == "1520700003"
    assert box_row.nba_stats_team_id == "45"
    assert box_row.raw_team_abbreviation == "CHN"
    assert box_row.pts == 77
    assert box_row.fgm is None


@pytest.mark.asyncio
async def test_normalize_competition_games_adds_gamelog_fallback_team_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing boxscore team rows fall back to season gamelog rows."""
    raw_run = SummerLeagueIngestionRun(
        id=1,
        year=2007,
        league_id="15",
        venue_slug="las_vegas",
        status=SummerLeagueRawRunStatus.COMPLETE,
        manifest_path="2007/15/manifest.json",
        game_count=2,
    )
    raw_files = [
        SummerLeagueSourceDocument(
            raw_run_id=1,
            year=2007,
            league_id="15",
            endpoint="boxscoretraditionalv2",
            game_id="G1",
            relative_path="2007/15/games/G1/boxscoretraditionalv2.json",
            parse_status=SummerLeagueRawFileStatus.PARSED,
        )
    ]
    competition = SummerLeagueEdition(
        id=10,
        year=2007,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2007 Las Vegas Summer League",
    )
    team_rows = [
        ParsedTeamGamelogRow("G1", None, "A", "Alpha", "ALP", "ALP vs. BET", 80),
        ParsedTeamGamelogRow("G1", None, "B", "Beta", "BET", "BET @ ALP", 70),
        ParsedTeamGamelogRow("G2", None, "C", "Gamma", "GAM", "GAM vs. DEL", 65),
    ]
    box_rows = [
        ParsedTeamBoxRow("G1", "A", "Alpha", "ALP", pts=80, fgm=30),
    ]
    team_entries = {
        "A": SummerLeagueTeamEntry(
            id=20,
            competition_id=10,
            nba_stats_team_id="A",
            raw_team_name="Alpha",
            team_slug="alpha",
        ),
        "B": SummerLeagueTeamEntry(
            id=21,
            competition_id=10,
            nba_stats_team_id="B",
            raw_team_name="Beta",
            team_slug="beta",
        ),
        "C": SummerLeagueTeamEntry(
            id=None,
            competition_id=10,
            nba_stats_team_id="C",
            raw_team_name="Gamma",
            team_slug="gamma",
        ),
    }
    games = {
        "G1": SummerLeagueGame(id=30, competition_id=10, nba_stats_game_id="G1"),
        "G2": SummerLeagueGame(id=31, competition_id=10, nba_stats_game_id="G2"),
    }
    calls: list[tuple[str, str, int | None]] = []

    class FakeDb:
        async def flush(self) -> None:
            return None

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            # refresh_competition_date_window (#6) runs one min/max query; this
            # fake reports no dated games, so the date window is left untouched.
            class _Result:
                def one(self) -> tuple[None, None]:
                    return (None, None)

                def first(self) -> tuple[None, None]:
                    return (None, None)

            return _Result()

    async def fake_get_raw_run(
        *_args: object, **_kwargs: object
    ) -> SummerLeagueIngestionRun:
        return raw_run

    async def fake_get_raw_files(
        *_args: object, **_kwargs: object
    ) -> list[SummerLeagueSourceDocument]:
        return raw_files

    async def fake_upsert_competition(
        *_args: object, **_kwargs: object
    ) -> SummerLeagueEdition:
        return competition

    async def fake_upsert_team_entry(
        _db: object,
        _competition_id: int,
        row: ParsedTeamGamelogRow,
    ) -> tuple[SummerLeagueTeamEntry, bool]:
        return team_entries[row.nba_stats_team_id], False

    async def fake_upsert_game(
        _db: object,
        _competition_id: int,
        game_id: str,
        *_args: object,
        **_kwargs: object,
    ) -> SummerLeagueGame:
        return games[game_id]

    async def fake_upsert_team_game_log(
        _db: object,
        _competition_id: int,
        _game_id: int,
        _team_entry_id: int,
        box_row: ParsedTeamBoxRow,
        *,
        source_endpoint: str = "boxscoretraditionalv2",
    ) -> SummerLeagueTeamGameLog:
        calls.append((source_endpoint, box_row.nba_stats_team_id, box_row.fgm))
        return SummerLeagueTeamGameLog(
            competition_id=10,
            game_id=_game_id,
            team_entry_id=_team_entry_id,
            source_endpoint=source_endpoint,
        )

    async def fake_refresh_competition_date_window(
        *_args: object, **_kwargs: object
    ) -> None:
        return None

    async def fake_empty_source_map(
        *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        # This fixture's teams/games are seeded entirely from this batch's
        # gamelog/box rows below (#633's full-competition seed has nothing to
        # add here); empty avoids requiring a real ``AsyncSession.scalars()``
        # on ``FakeDb``.
        return {}

    monkeypatch.setattr(service, "_get_raw_run", fake_get_raw_run)
    monkeypatch.setattr(service, "_get_raw_files", fake_get_raw_files)
    monkeypatch.setattr(service, "_upsert_competition", fake_upsert_competition)
    monkeypatch.setattr(service, "_limited_game_ids", lambda **_kwargs: None)
    monkeypatch.setattr(service, "parse_team_gamelog", lambda _path: team_rows)
    monkeypatch.setattr(
        service, "parse_team_box_rows", lambda *_args, **_kwargs: box_rows
    )
    monkeypatch.setattr(service, "_upsert_team_entry", fake_upsert_team_entry)
    monkeypatch.setattr(service, "_upsert_game", fake_upsert_game)
    monkeypatch.setattr(service, "_upsert_team_game_log", fake_upsert_team_game_log)
    monkeypatch.setattr(
        service, "refresh_competition_date_window", fake_refresh_competition_date_window
    )
    monkeypatch.setattr(service, "_teams_by_source_id", fake_empty_source_map)
    monkeypatch.setattr(service, "_games_by_source_id", fake_empty_source_map)

    report = await normalize_competition_games(
        FakeDb(),  # type: ignore[arg-type]
        year=2007,
        league_id="15",
        raw_root=tmp_path,
    )

    assert report.team_game_logs_upserted == 2
    assert calls == [
        ("boxscoretraditionalv2", "A", 30),
        ("leaguegamelog_team", "B", None),
    ]


# ---------------------------------------------------------------------------
# _upsert_team_entry -- dual-read target resolution (#796)
# ---------------------------------------------------------------------------


class _FakeExecuteResult:
    def __init__(self, row: SummerLeagueTeamEntry | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> SummerLeagueTeamEntry | None:
        return self._row


class _FakeUpsertSession:
    """Minimal AsyncSession stand-in: one canned SELECT result, no writes."""

    def __init__(self, existing_row: SummerLeagueTeamEntry | None = None) -> None:
        self.existing_row = existing_row
        self.added: list[object] = []

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeExecuteResult:
        return _FakeExecuteResult(self.existing_row)

    def add(self, obj: object) -> None:
        self.added.append(obj)


def _gamelog_row(nba_stats_team_id: str = "1610612747") -> ParsedTeamGamelogRow:
    return ParsedTeamGamelogRow(
        "G1", None, nba_stats_team_id, "Los Angeles Lakers", "LAL", "LAL vs. BOS", 100
    )


@pytest.mark.asyncio
async def test_upsert_team_entry_new_row_resolves_both_targets_on_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating a team entry resolves and stores both dual-read targets."""

    async def fake_resolve(
        _db: object, *, nba_stats_team_id: str
    ) -> tuple[int | None, int | None]:
        assert nba_stats_team_id == "1610612747"
        return (5, 900)

    monkeypatch.setattr(service, "resolve_team_targets", fake_resolve)
    db = _FakeUpsertSession(existing_row=None)

    row, created_unresolved = await service._upsert_team_entry(
        db, 10, _gamelog_row()
    )

    assert row.nba_team_id == 5
    assert row.team_program_id == 900
    assert created_unresolved is False


@pytest.mark.asyncio
async def test_upsert_team_entry_new_row_stays_null_when_resolution_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-NBA/select squad creates a row with both targets left NULL."""

    async def fake_resolve(
        _db: object, *, nba_stats_team_id: str
    ) -> tuple[int | None, int | None]:
        return (None, None)

    monkeypatch.setattr(service, "resolve_team_targets", fake_resolve)
    db = _FakeUpsertSession(existing_row=None)

    row, created_unresolved = await service._upsert_team_entry(
        db, 10, _gamelog_row(nba_stats_team_id="orlando-white")
    )

    assert row.nba_team_id is None
    assert row.team_program_id is None
    assert created_unresolved is True


@pytest.mark.asyncio
async def test_upsert_team_entry_fills_null_target_without_overwriting_set_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing row with one target already set never has it overwritten."""
    existing = SummerLeagueTeamEntry(
        id=20,
        competition_id=10,
        nba_stats_team_id="1610612747",
        raw_team_name="Los Angeles Lakers",
        team_slug="los-angeles-lakers",
        nba_team_id=None,
        team_program_id=77,
    )

    async def fake_resolve(
        _db: object, *, nba_stats_team_id: str
    ) -> tuple[int | None, int | None]:
        return (5, 999)

    monkeypatch.setattr(service, "resolve_team_targets", fake_resolve)
    db = _FakeUpsertSession(existing_row=existing)

    row, created_unresolved = await service._upsert_team_entry(db, 10, _gamelog_row())

    assert row.nba_team_id == 5  # filled -- was NULL
    assert row.team_program_id == 77  # untouched -- was already set
    assert created_unresolved is False


@pytest.mark.asyncio
async def test_upsert_team_entry_skips_resolution_once_both_targets_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-resolved existing row never re-queries the resolver."""
    existing = SummerLeagueTeamEntry(
        id=20,
        competition_id=10,
        nba_stats_team_id="1610612747",
        raw_team_name="Los Angeles Lakers",
        team_slug="los-angeles-lakers",
        nba_team_id=5,
        team_program_id=900,
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resolve_team_targets must not be called")

    monkeypatch.setattr(service, "resolve_team_targets", fail)
    db = _FakeUpsertSession(existing_row=existing)

    row, created_unresolved = await service._upsert_team_entry(db, 10, _gamelog_row())

    assert row.nba_team_id == 5
    assert row.team_program_id == 900
    assert created_unresolved is False

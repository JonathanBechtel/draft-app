"""Unit tests for #541's Live hero running-line + top-performer helpers.

`_hero_line_from_logs` and `_top_performers_from_logs` (`app.services.summer_league.desk_read`)
are pure -- both take an already-fetched `{game_id: [log rows]}` map (the shared,
single-query fetch `_fetch_game_logs_for_games` does at request time) and never touch a
session, so they're exercised directly here with plain `SummerLeaguePlayerGameLog`
instances rather than a database fixture. See
``tests/integration/test_sl_desk_home.py`` for the end-to-end DB-backed read path.
"""

from __future__ import annotations

from app.schemas.summer_league import SummerLeaguePlayerGameLog
from app.services.summer_league.desk_read import (
    _hero_line_from_logs,
    _top_performers_from_logs,
)


def _log(
    *, game_id: int, player_id: int, pts: int, reb: int, ast: int
) -> SummerLeaguePlayerGameLog:
    return SummerLeaguePlayerGameLog(
        competition_id=1,
        game_id=game_id,
        team_entry_id=1,
        source_player_id=1,
        player_id=player_id,
        nba_stats_person_id=f"p{player_id}",
        raw_player_name=f"Player {player_id}",
        pts=pts,
        reb=reb,
        ast=ast,
    )


# --------------------------------------------------------------------------- #
# _hero_line_from_logs
# --------------------------------------------------------------------------- #
def test_hero_line_from_logs_returns_real_pts_reb_ast_gmsc() -> None:
    logs_by_game = {8: [_log(game_id=8, player_id=101, pts=18, reb=6, ast=4)]}
    line = _hero_line_from_logs(logs_by_game, game_id=8, player_id=101)
    assert line is not None
    assert (line.pts, line.reb, line.ast) == (18, 6, 4)
    # GmSc (`game_score_from_row`) only weights ast (0.7) among this fixture's
    # non-zero fields -- `reb` (total rebounds) isn't a GmSc term at all
    # (oreb/dreb are): 18 + 0.7*4 == 20.8.
    assert line.gmsc == 20.8


def test_hero_line_from_logs_pretip_subject_gets_all_none_line_not_missing_object() -> None:
    """A real subject with no logged row yet renders every field em-dash, not a dropped line."""
    line = _hero_line_from_logs({}, game_id=8, player_id=101)
    assert line is not None
    assert (line.pts, line.reb, line.ast, line.gmsc) == (None, None, None, None)


def test_hero_line_from_logs_no_subject_returns_none_outright() -> None:
    """A single-subject hero's absent second subject gets `None`, not an empty line."""
    assert _hero_line_from_logs({8: []}, game_id=8, player_id=None) is None


def test_hero_line_from_logs_never_returns_a_different_players_line() -> None:
    logs_by_game = {
        8: [
            _log(game_id=8, player_id=101, pts=18, reb=6, ast=4),
            _log(game_id=8, player_id=102, pts=30, reb=10, ast=8),
        ]
    }
    line = _hero_line_from_logs(logs_by_game, game_id=8, player_id=101)
    assert line is not None
    assert line.pts == 18  # not player 102's 30


def test_hero_line_from_logs_wrong_game_id_degrades_to_pretip_line() -> None:
    logs_by_game = {8: [_log(game_id=8, player_id=101, pts=18, reb=6, ast=4)]}
    line = _hero_line_from_logs(logs_by_game, game_id=9, player_id=101)
    assert line is not None
    assert line.pts is None


# --------------------------------------------------------------------------- #
# _top_performers_from_logs
# --------------------------------------------------------------------------- #
def test_top_performers_from_logs_picks_highest_gmsc_per_game() -> None:
    logs_by_game = {
        8: [
            _log(game_id=8, player_id=101, pts=18, reb=6, ast=4),
            _log(game_id=8, player_id=102, pts=30, reb=10, ast=8),
        ],
        9: [_log(game_id=9, player_id=201, pts=12, reb=3, ast=2)],
    }
    top = _top_performers_from_logs(logs_by_game)
    assert top[8] == (102, 35.6)  # 30 + 0.7*8 ast beats 18 + 0.7*4 ast
    assert top[9] == (201, 13.4)  # 12 + 0.7*2 ast


def test_top_performers_from_logs_empty_input_returns_empty_dict() -> None:
    assert _top_performers_from_logs({}) == {}

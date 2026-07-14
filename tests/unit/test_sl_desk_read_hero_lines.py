"""Unit tests for #541's Live hero running-line + top-performer helpers.

`_hero_line_from_logs` and `_top_performers_from_logs` (`app.services.summer_league.desk_read`)
are pure -- both take an already-fetched `{game_id: [log rows]}` map (the shared,
single-query fetch `_fetch_game_logs_for_games` does at request time) and never touch a
session, so they're exercised directly here with plain `SummerLeaguePlayerGameLog`
instances rather than a database fixture. See
``tests/integration/test_sl_desk_home.py`` for the end-to-end DB-backed read path.
"""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.summer_league import (
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
)
from app.schemas.summer_league_desk import SummerLeagueDeskSlate
from app.services.summer_league.desk_read import (
    _effective_game_status,
    _hero_line_from_logs,
    _live_hero_headline,
    _pick_hero_slate_row,
    _played,
    _top_performers_from_logs,
)


def _log(
    *,
    game_id: int,
    player_id: int,
    pts: int,
    reb: int,
    ast: int,
    minutes_seconds: int | None = 1200,
) -> SummerLeaguePlayerGameLog:
    return SummerLeaguePlayerGameLog(
        competition_id=1,
        game_id=game_id,
        team_entry_id=1,
        source_player_id=1,
        player_id=player_id,
        nba_stats_person_id=f"p{player_id}",
        raw_player_name=f"Player {player_id}",
        minutes_seconds=minutes_seconds,
        pts=pts,
        reb=reb,
        ast=ast,
    )


def _dnp(*, game_id: int, player_id: int) -> SummerLeaguePlayerGameLog:
    """A DNP/pre-tip roster shell: on the roster, NULL minutes and box stats."""
    return SummerLeaguePlayerGameLog(
        competition_id=1,
        game_id=game_id,
        team_entry_id=1,
        source_player_id=1,
        player_id=player_id,
        nba_stats_person_id=f"p{player_id}",
        raw_player_name=f"Player {player_id}",
        minutes_seconds=None,
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


def test_hero_line_from_logs_pretip_subject_gets_all_none_line_not_missing_object() -> (
    None
):
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


def test_scheduled_game_past_tip_displays_in_progress_at_snapshot_time() -> None:
    """The board applies the same past-tip live fallback as the Desk state machine."""
    game = SummerLeagueGame(
        competition_id=1,
        nba_stats_game_id="late-status",
        tip_datetime=datetime(2026, 7, 14, 1, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )

    assert (
        _effective_game_status(game, now=datetime(2026, 7, 14, 1, 18))
        == SummerLeagueGameStatus.IN_PROGRESS
    )


def test_live_hero_headline_uses_tonights_same_game_score() -> None:
    """Live headline GmSc comes from the displayed box line, never an event average."""
    line = _hero_line_from_logs(
        {8: [_log(game_id=8, player_id=101, pts=18, reb=6, ast=4)]},
        game_id=8,
        player_id=101,
    )

    assert _live_hero_headline("MIA @ CLE", line, None) == (
        "Live Game Score: 20.8 in MIA @ CLE."
    )


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


# --------------------------------------------------------------------------- #
# _played / DNP-shell exclusion
# --------------------------------------------------------------------------- #
def test_played_true_for_real_minutes_false_for_dnp_shell() -> None:
    assert _played(_log(game_id=8, player_id=1, pts=0, reb=0, ast=0)) is True
    assert _played(_dnp(game_id=8, player_id=1)) is False
    # A zero-minute line (checked in, subbed out immediately) is not a
    # performance either.
    assert (
        _played(_log(game_id=8, player_id=1, pts=0, reb=0, ast=0, minutes_seconds=0))
        is False
    )


def test_top_performers_from_logs_skips_dnp_shells() -> None:
    """A DNP shell (0.0 GmSc) must never win the top-performer slot over a real line."""
    logs_by_game = {
        8: [
            _dnp(game_id=8, player_id=999),  # rostered veteran, sat -> GmSc 0.0
            _log(game_id=8, player_id=101, pts=9, reb=3, ast=2),
        ]
    }
    top = _top_performers_from_logs(logs_by_game)
    assert top[8] == (101, 10.4)  # 9 + 0.7*2, not the DNP's 0.0


def test_top_performers_from_logs_all_dnp_game_has_no_performer() -> None:
    """A pre-tip game with only roster shells yields no top performer, not a 0.0 one."""
    logs_by_game = {8: [_dnp(game_id=8, player_id=1), _dnp(game_id=8, player_id=2)]}
    assert _top_performers_from_logs(logs_by_game) == {}


# --------------------------------------------------------------------------- #
# _pick_hero_slate_row -- empty in-progress game must not headline
# --------------------------------------------------------------------------- #
def _slate(game_id: int, *, is_hero: bool, rank: int) -> SummerLeagueDeskSlate:
    return SummerLeagueDeskSlate(
        game_date=date(2026, 7, 10),
        competition_id=1,
        game_id=game_id,
        is_hero=is_hero,
        rank=rank,
        total_weight=100.0,
    )


def _game(game_id: int, status: SummerLeagueGameStatus) -> SummerLeagueGame:
    return SummerLeagueGame(
        competition_id=1, nba_stats_game_id=f"g{game_id}", status=status
    )


def test_pick_hero_skips_in_progress_game_with_no_box_data() -> None:
    """A stale/just-tipped in-progress game (no box line) must not win the Live hero.

    Game 8 is flagged in_progress but has logged nothing; game 9 is final with a
    real line. Even though game 8 carries the tick's ``is_hero`` flag, the Live
    hero falls back to game 9 so it never headlines em-dash lines.
    """
    rows = [_slate(8, is_hero=True, rank=1), _slate(9, is_hero=False, rank=2)]
    games = {
        8: _game(8, SummerLeagueGameStatus.IN_PROGRESS),
        9: _game(9, SummerLeagueGameStatus.FINAL),
    }
    logs = {9: [_log(game_id=9, player_id=1, pts=20, reb=5, ast=3)]}  # game 8: none
    picked = _pick_hero_slate_row(rows, games, live=True, logs_by_game=logs)
    assert picked is not None and picked.game_id == 9


def test_pick_hero_prefers_in_progress_game_that_has_box_data() -> None:
    """An in-progress game WITH a real line beats a final one, even a flagged hero."""
    rows = [_slate(8, is_hero=False, rank=2), _slate(9, is_hero=True, rank=1)]
    games = {
        8: _game(8, SummerLeagueGameStatus.IN_PROGRESS),
        9: _game(9, SummerLeagueGameStatus.FINAL),
    }
    logs = {
        8: [_log(game_id=8, player_id=1, pts=15, reb=4, ast=2)],
        9: [_log(game_id=9, player_id=2, pts=25, reb=6, ast=4)],
    }
    picked = _pick_hero_slate_row(rows, games, live=True, logs_by_game=logs)
    assert picked is not None and picked.game_id == 8


def test_pick_hero_falls_back_to_is_hero_when_no_game_has_data() -> None:
    """Before any line is logged event-wide, the tick's own is_hero still wins (pre-tip)."""
    rows = [_slate(8, is_hero=True, rank=2), _slate(9, is_hero=False, rank=1)]
    games = {
        8: _game(8, SummerLeagueGameStatus.IN_PROGRESS),
        9: _game(9, SummerLeagueGameStatus.SCHEDULED),
    }
    picked = _pick_hero_slate_row(rows, games, live=True, logs_by_game={})
    assert picked is not None and picked.game_id == 8

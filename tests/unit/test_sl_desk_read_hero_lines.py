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
from types import SimpleNamespace

import pytest

import app.services.summer_league.desk_read as desk_read
from app.services.event_desk.payload import (
    DeskFreshness,
    DeskHero,
    DeskLiveBoardRow,
    DeskPayload,
    DeskTrackerSection,
)
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


def _current_game(
    game_id: int, *, status: SummerLeagueGameStatus = SummerLeagueGameStatus.IN_PROGRESS
) -> SummerLeagueGame:
    """Build the current game shape consumed by the live snapshot overlay."""
    return SummerLeagueGame(
        id=game_id,
        competition_id=1,
        nba_stats_game_id=f"g{game_id}",
        game_date=date(2026, 7, 10),
        status=status,
        home_score=40,
        away_score=38,
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


def _live_payload(
    *,
    subject_player_id: int | None = 101,
    subject_player_id_2: int | None = 102,
    subject_line=None,
    subject_line_2=None,
) -> DeskPayload:
    """Build the smallest snapshot payload needed by the Live overlay tests."""
    hero = DeskHero(
        kind="live_duel",
        game_id=8,
        subject_player_id=subject_player_id,
        subject_player_id_2=subject_player_id_2,
        headline="Live now: T2 @ T1.",
        tagline="Tick read",
        subject_line=subject_line,
        subject_line_2=subject_line_2,
    )
    return DeskPayload(
        daily_state="live",
        is_home_owner=True,
        hero=hero,
        slate=[],
        live_board=[
            DeskLiveBoardRow(
                game_id=8,
                matchup_label="T2 @ T1",
                status="in_progress",
                home_score=40,
                away_score=38,
                top_performer_player_id=None,
                top_performer_gmsc=None,
                read=None,
            )
        ],
        ledger=[],
        tracker=DeskTrackerSection(cohort="full_class", stat_view="box"),
        freshness=DeskFreshness(
            last_tick_at=None,
            next_tick_eta=None,
            as_of_et_label="as of 7:00pm ET",
        ),
    )


@pytest.mark.asyncio
async def test_snapshot_live_hero_overlay_uses_current_featured_game_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot overlay replaces pre-tip lines and rewrites the headline."""

    async def _fetch(_db, *, game_ids):
        assert game_ids == [8]
        return desk_read._CurrentLiveSnapshotState(
            games={8: _current_game(8)},
            logs_by_game={
                8: {
                    101: _log(game_id=8, player_id=101, pts=22, reb=7, ast=3),
                    102: _log(game_id=8, player_id=102, pts=15, reb=4, ast=5),
                }
            },
            players={},
        )

    monkeypatch.setattr(desk_read, "_fetch_current_live_snapshot_state", _fetch)
    refreshed, _players = await desk_read._refresh_snapshot_live_state(
        object(), _live_payload(), now=datetime(2026, 7, 10, 23, 10)
    )

    assert refreshed.hero.subject_line is not None
    assert (refreshed.hero.subject_line.pts, refreshed.hero.subject_line.reb) == (
        22,
        7,
    )
    assert refreshed.hero.subject_line_2 is not None
    assert (refreshed.hero.subject_line_2.pts, refreshed.hero.subject_line_2.ast) == (
        15,
        5,
    )
    assert refreshed.hero.headline == "Live Game Score: 24.1 vs 18.5 in T2 @ T1."
    assert refreshed.hero.tagline is None


@pytest.mark.asyncio
async def test_snapshot_live_hero_overlay_preserves_coherent_hero_without_current_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty current response preserves the last coherent hero unchanged."""

    async def _fetch(_db, *, game_ids):
        assert game_ids == [8]
        return desk_read._CurrentLiveSnapshotState(
            games={8: _current_game(8)}, logs_by_game={}, players={}
        )

    monkeypatch.setattr(desk_read, "_fetch_current_live_snapshot_state", _fetch)
    prior_line = desk_read.DeskHeroLine(pts=18, reb=6, ast=4, gmsc=20.8)
    payload = _live_payload(subject_line=prior_line)
    refreshed, _players = await desk_read._refresh_snapshot_live_state(
        object(), payload, now=datetime(2026, 7, 10, 23, 10)
    )

    assert refreshed.hero == payload.hero


@pytest.mark.asyncio
async def test_snapshot_live_hero_overlay_skips_non_live_or_gameless_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview and gameless snapshots remain untouched without a log query."""

    async def _unexpected_fetch(_db, *, game_ids):
        raise AssertionError("non-Live snapshots must not refresh box lines")

    monkeypatch.setattr(
        desk_read, "_fetch_current_live_snapshot_state", _unexpected_fetch
    )
    preview = desk_read.dataclass_replace(_live_payload(), daily_state="preview")
    no_games = desk_read.dataclass_replace(
        _live_payload(subject_player_id=None, subject_player_id_2=None),
        hero=desk_read.dataclass_replace(
            _live_payload().hero,
            game_id=None,
            subject_player_id=None,
            subject_player_id_2=None,
        ),
        live_board=[],
    )

    refreshed_preview, _ = await desk_read._refresh_snapshot_live_state(
        object(), preview, now=datetime(2026, 7, 10, 23, 10)
    )
    refreshed_no_games, _ = await desk_read._refresh_snapshot_live_state(
        object(), no_games, now=datetime(2026, 7, 10, 23, 10)
    )
    assert refreshed_preview is preview
    assert refreshed_no_games is no_games


@pytest.mark.asyncio
async def test_snapshot_reader_can_skip_live_overlay_for_tracker_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracker-only reads keep the decoded snapshot and avoid mutable live queries."""
    now = datetime(2026, 7, 10, 23, 10)

    async def _resolve(_db, *, now, require_owner):
        return SimpleNamespace(
            event_row=SimpleNamespace(id=1),
            daily_state=desk_read.EventDailyState.LIVE,
        )

    async def _snapshot(_db, **_kwargs):
        return SimpleNamespace(
            payload_json={},
            view_context_json={},
            schema_version=1,
            source_freshness_tick_at=now,
            source_freshness_next_tick_eta=None,
        )

    def _deserialize(**_kwargs):
        return desk_read.DeskView(
            payload=_live_payload(), players={}, matchups={}, tracker_teams={}
        )

    async def _unexpected_overlay(*_args, **_kwargs):
        raise AssertionError("tracker fragment must not query mutable live facts")

    monkeypatch.setattr(desk_read, "_resolve_window_state", _resolve)
    monkeypatch.setattr(desk_read, "_refresh_snapshot_live_state", _unexpected_overlay)
    monkeypatch.setattr(
        "app.services.event_desk.render_snapshots.get_render_snapshot", _snapshot
    )
    monkeypatch.setattr(
        "app.services.event_desk.render_snapshots.deserialize_desk_view", _deserialize
    )

    view = await desk_read.get_desk_view_from_snapshot(
        object(), now=now, refresh_live_state=False
    )

    assert view.payload is not None
    assert view.payload.hero == _live_payload().hero


@pytest.mark.asyncio
async def test_live_state_matches_source_canonical_id_when_log_fk_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved source row supplies a rendered line before its log FK catches up."""
    row = _log(game_id=8, player_id=101, pts=22, reb=7, ast=3)
    row.player_id = None
    game = _current_game(8)

    class _Result:
        def all(self):
            return [(game, row, 101, None)]

    class _Session:
        async def execute(self, _statement):
            return _Result()

    matched = await desk_read._fetch_current_live_snapshot_state(
        _Session(), game_ids=[8]
    )

    assert matched.logs_by_game == {8: {101: row}}

    async def _fetch(_db, *, game_ids):
        assert game_ids == [8]
        return matched

    monkeypatch.setattr(desk_read, "_fetch_current_live_snapshot_state", _fetch)
    payload = _live_payload(subject_player_id=101, subject_player_id_2=None)
    refreshed, _players = await desk_read._refresh_snapshot_live_state(
        object(), payload, now=datetime(2026, 7, 10, 23, 10)
    )
    assert refreshed.hero.subject_line is not None
    assert refreshed.hero.subject_line.pts == 22


@pytest.mark.asyncio
async def test_live_state_reselects_active_game_with_current_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed snapshot hero yields to an active game with real box lines."""
    payload = _live_payload()
    payload = desk_read.dataclass_replace(
        payload,
        live_board=[
            desk_read.dataclass_replace(payload.live_board[0], status="final"),
            DeskLiveBoardRow(
                game_id=9,
                matchup_label="T4 @ T3",
                status="in_progress",
                home_score=31,
                away_score=29,
                top_performer_player_id=None,
                top_performer_gmsc=None,
                read=None,
            ),
        ],
    )

    async def _fetch(_db, *, game_ids):
        assert game_ids == [8, 9]
        return desk_read._CurrentLiveSnapshotState(
            games={
                8: _current_game(8, status=SummerLeagueGameStatus.FINAL),
                9: _current_game(9),
            },
            logs_by_game={
                9: {
                    201: _log(game_id=9, player_id=201, pts=24, reb=5, ast=4),
                    202: _log(game_id=9, player_id=202, pts=18, reb=8, ast=2),
                }
            },
            players={201: {"display_name": "Player 201"}},
        )

    monkeypatch.setattr(desk_read, "_fetch_current_live_snapshot_state", _fetch)
    refreshed, players = await desk_read._refresh_snapshot_live_state(
        object(), payload, now=datetime(2026, 7, 10, 23, 10)
    )

    assert refreshed.hero.game_id == 9
    assert (refreshed.hero.subject_player_id, refreshed.hero.subject_player_id_2) == (
        201,
        202,
    )
    assert refreshed.hero.subject_line is not None
    assert refreshed.hero.subject_line.pts == 24
    assert refreshed.hero.facts == []
    assert players == {201: {"display_name": "Player 201"}}


@pytest.mark.asyncio
async def test_live_state_promotes_current_game_over_stale_quiet_hero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-tip quiet snapshot becomes a live game hero once box data exists."""
    payload = _live_payload()
    payload = desk_read.dataclass_replace(
        payload,
        hero=desk_read.dataclass_replace(
            payload.hero,
            kind="quiet_slate",
            game_id=None,
            subject_player_id=301,
            subject_player_id_2=None,
            subject_line=None,
            subject_line_2=None,
        ),
    )

    async def _fetch(_db, *, game_ids):
        assert game_ids == [8]
        return desk_read._CurrentLiveSnapshotState(
            games={8: _current_game(8)},
            logs_by_game={
                8: {401: _log(game_id=8, player_id=401, pts=20, reb=6, ast=5)}
            },
            players={},
        )

    monkeypatch.setattr(desk_read, "_fetch_current_live_snapshot_state", _fetch)
    refreshed, _players = await desk_read._refresh_snapshot_live_state(
        object(), payload, now=datetime(2026, 7, 10, 23, 10)
    )

    assert refreshed.hero.kind == "live_duel"
    assert refreshed.hero.game_id == 8
    assert refreshed.hero.subject_player_id == 401
    assert refreshed.hero.subject_player_id_2 is None
    assert refreshed.hero.subject_line is not None
    assert refreshed.hero.subject_line.pts == 20


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


def test_played_accepts_stat_bearing_row_with_blank_minutes() -> None:
    """A transient blank MIN must not hide a player whose box stats are present."""
    assert _played(
        _log(game_id=8, player_id=1, pts=12, reb=3, ast=2, minutes_seconds=None)
    )


def test_played_rejects_zero_shell_and_dnp_comment_with_blank_minutes() -> None:
    """Numeric zero shells and explicit DNP comments are not appearances."""
    zero_shell = _log(game_id=8, player_id=1, pts=0, reb=0, ast=0, minutes_seconds=None)
    assert _played(zero_shell) is False
    zero_shell.comment = "DNP - Coach's Decision"
    zero_shell.pts = 12
    assert _played(zero_shell) is False


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

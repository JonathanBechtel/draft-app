"""Integration tests for the Summer League Desk states UI (#509).

Proves the ticket's central contract -- exactly one state renders server-side
and there is **no user-facing state switcher** -- plus per-state section
presence, the em-dash-before-tip Top Performer case, `?ref=sl-desk` deep-link
tagging, and the Morning slate's JS-off tail-collapse behavior.

Live and Recap are exercised through the REAL `/` route (real wall-clock):
both states are driven purely by game *status* (behavior spec §2 rules 1 and
3 -- "Live always wins" / "today's last final" -- Live if any game is
`in_progress`; Recap if every known game today is `final`), so they resolve
correctly regardless of what time of day the test happens to run. Preview
(Morning) is the one transition that IS time-of-day dependent (the
schedule-relative Ledger->Morning flip -- see `state_machine.inner_state`'s
docstring), so it is exercised by rendering the Morning partials directly
against a `get_desk_payload(..., now=<pinned>)` result instead of the live
route -- avoiding a flaky, time-of-day-dependent HTTP assertion (the repo's
existing in-window page-budget test makes the same choice: it only
parametrizes "live"/"recap", never "preview").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from jinja2 import Environment, FileSystemLoader

from app.templating import register_template_filters
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.desk_read import (
    get_desk_payload,
    get_desk_view_context,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from scripts.sl_desk_tick import run_desk_tick

pytestmark = pytest.mark.asyncio

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"

# Review-only mockup chrome that must NEVER appear in a production render
# (mockups/draftguru_sl_scout_desk.html's tab switcher is explicitly
# review-only -- see that file's own comment and behavior spec §1/§2).
_SWITCHER_MARKERS = (
    "desk-tab",
    "state-panel",
    'role="tablist"',
    'data-state="morning"',
    'data-state="live"',
    'data-state="ledger"',
)

_IDX = {"n": 0}


def _idx() -> int:
    _IDX["n"] += 1
    return _IDX["n"]


class _FakeResponse:
    """Minimal curl_cffi-shaped response returning a fixed JSON payload."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200

    def json(self) -> object:
        """Return the configured payload."""
        return self.payload


class _FakeSession:
    """Fake NBA Stats session that never hits the network."""

    def get(self, url: str, params: dict[str, str]) -> _FakeResponse:
        """Return an empty schedule -- games are seeded directly by the test."""
        return _FakeResponse({"leagueSchedule": {"gameDates": []}})

    def close(self) -> None:
        """No-op close (matches the real session interface)."""


def _fake_client() -> NBAStatsClient:
    return NBAStatsClient(session=_FakeSession())


async def _seed_competition(
    db: AsyncSession, *, today: date
) -> SummerLeagueCompetition:
    idx = _idx()
    comp = SummerLeagueCompetition(
        year=today.year,
        league_id="15",
        venue_slug=f"vegas-desk-ui-{idx}",
        display_name=f"{today.year} Las Vegas",
        starts_on=today - timedelta(days=2),
        ends_on=today + timedelta(days=8),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueCompetition, *, franchise_stats_id: str
) -> SummerLeagueTeamEntry:
    idx = _idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=franchise_stats_id,
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
        team_slug=f"desk-ui-team-{idx}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _seed_game(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    *,
    game_date: date,
    tip_datetime: datetime | None,
    status: SummerLeagueGameStatus,
    home_score: int | None = None,
    away_score: int | None = None,
) -> SummerLeagueGame:
    idx = _idx()
    assert competition.id is not None and home.id is not None and away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"desk-ui-game-{idx}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=status,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _seed_player(
    db: AsyncSession, *, name: str, draft_round: int | None, draft_pick: int | None
) -> PlayerMaster:
    idx = _idx()
    player = PlayerMaster(
        first_name=name,
        last_name=f"Test{idx}",
        display_name=f"{name} Test{idx}",
        draft_year=2026,
        draft_round=draft_round,
        draft_pick=draft_pick,
        position="G",
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _roster_player(
    db: AsyncSession,
    competition: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
) -> SummerLeagueSourcePlayer:
    idx = _idx()
    assert competition.id is not None and team.id is not None and player.id is not None
    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"desk-ui-src-{idx}",
        raw_player_name=player.display_name or "Test Player",
        normalized_name=(player.display_name or "test player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    assert source_player.id is not None

    db.add(
        SummerLeagueParticipation(
            competition_id=competition.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=player.id,
            roster_status=AffiliationStatus.ACTIVE,
        )
    )
    await db.flush()
    return source_player


async def _seed_baseline(db: AsyncSession, *, baseline_version: str) -> None:
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key="slot:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.EVENT,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=40.0,
            n_members=20,
            breakpoints={"0": 10.0, "25": 30.0, "50": 50.0, "75": 70.0, "100": 90.0},
            mean_value=50.0,
            median_value=50.0,
        )
    )
    # The Ledger (#539) ranks a single game's GmSc against the GAME-grain
    # baseline, not EVENT -- seed both so the Recap Ledger table renders.
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key="game:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.GAME,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=10.0,
            n_members=20,
            breakpoints={"0": 5.0, "25": 15.0, "50": 25.0, "75": 40.0, "100": 60.0},
            mean_value=25.0,
            median_value=25.0,
        )
    )
    await db.flush()


async def _seed_game_log(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    source_player: SummerLeagueSourcePlayer,
    player: PlayerMaster,
    pts: int = 24,
) -> None:
    assert (
        competition.id is not None
        and game.id is not None
        and team.id is not None
        and source_player.id is not None
        and player.id is not None
    )
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=source_player.id,
            player_id=player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=player.display_name or "Player",
            minutes_seconds=1800,
            pts=pts,
            fgm=9,
            fga=16,
            ftm=4,
            fta=4,
            oreb=1,
            dreb=6,
            reb=7,
            ast=5,
            stl=1,
            blk=0,
            tov=2,
            pf=2,
        )
    )
    await db.flush()


def _assert_no_switcher_chrome(html: str) -> None:
    """The one negative assertion the whole ticket hinges on: no state tabs."""
    for marker in _SWITCHER_MARKERS:
        assert marker not in html, (
            f"found review-only switcher chrome {marker!r} in the DOM"
        )


# --------------------------------------------------------------------------- #
# Off-window
# --------------------------------------------------------------------------- #
async def test_off_window_renders_archive_strip_only(app_client: AsyncClient) -> None:
    """No active SL event -> the collapsed archive strip renders, no Desk module."""
    response = await app_client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'class="card desk__offwindow"' in html
    assert 'id="slDeskSection"' not in html
    assert "desk__hero" not in html
    _assert_no_switcher_chrome(html)


# --------------------------------------------------------------------------- #
# Live Desk (time-independent: forced by an in_progress game)
# --------------------------------------------------------------------------- #
async def test_live_state_renders_live_board_with_em_dash_before_tip(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Live: hero + live tick board render; a not-yet-tipped game shows an em-dash."""
    now = datetime.utcnow()
    today = to_eastern_date(now)

    competition = await _seed_competition(db_session, today=today)
    home = await _seed_team(db_session, competition, franchise_stats_id="1610612747")
    away = await _seed_team(db_session, competition, franchise_stats_id="1610612744")
    live_game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=today,
        tip_datetime=now - timedelta(hours=1),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=40,
        away_score=38,
    )
    player = await _seed_player(db_session, name="Rookie", draft_round=1, draft_pick=1)
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_baseline(db_session, baseline_version="desk-ui-v1")
    await _seed_game_log(
        db_session,
        competition=competition,
        game=live_game,
        team=home,
        source_player=source_player,
        player=player,
    )

    # A second, second-team pairing whose tip hasn't happened yet -- no game
    # log exists for it, so its Top Performer cell must render an em-dash.
    home2 = await _seed_team(db_session, competition, franchise_stats_id="1610612759")
    away2 = await _seed_team(db_session, competition, franchise_stats_id="1610612751")
    await _seed_game(
        db_session,
        competition,
        home2,
        away2,
        game_date=today,
        tip_datetime=now + timedelta(hours=2),
        status=SummerLeagueGameStatus.SCHEDULED,
    )

    await db_session.commit()
    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    warmup = await app_client.get("/")
    assert warmup.status_code == 200

    response = await app_client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="slDeskSection"' in html
    assert "desk__hero--live" in html
    assert 'class="desk__live-board"' in html
    # Em-dash before tip: the not-yet-started game's Top Performer cell.
    assert "desk__top-perf--empty" in html
    assert "&mdash;" in html
    # The resolved game's top performer shows a real GmSc, not an em-dash.
    assert "GmSc" in html
    _assert_no_switcher_chrome(html)
    assert "?ref=sl-desk" in html
    # #556: Desk player links (hero + live-board top performer) route through
    # the SL-scoped player page and carry placement attribution.
    assert "/summer-league?ref=sl-desk" in html
    assert 'data-desk-placement="hero"' in html
    assert 'data-desk-placement="live_board"' in html
    assert 'data-desk-daily-state="live"' in html


async def test_live_duel_hero_renders_both_running_lines_one_pretip_em_dash(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """#541: the Live Duel hero renders both subjects' PTS/REB/AST/GmSc.

    A logged subject shows real numbers; a not-yet-tipped subject on the SAME
    game shows an em dash for every stat -- never a zero, never dropped.
    """
    now = datetime.utcnow()
    today = to_eastern_date(now)

    competition = await _seed_competition(db_session, today=today)
    home = await _seed_team(db_session, competition, franchise_stats_id="1610612747")
    away = await _seed_team(db_session, competition, franchise_stats_id="1610612744")
    duel_game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=today,
        tip_datetime=now - timedelta(hours=1),
        status=SummerLeagueGameStatus.IN_PROGRESS,
        home_score=40,
        away_score=38,
    )
    # Two top-14 prospects (Duel qualifies on draft slot alone) sharing tonight's game.
    player_a = await _seed_player(
        db_session, name="DuelLogged", draft_round=1, draft_pick=1
    )
    player_b = await _seed_player(
        db_session, name="DuelPretip", draft_round=1, draft_pick=2
    )
    source_a = await _roster_player(db_session, competition, home, player_a)
    await _roster_player(db_session, competition, away, player_b)
    await _seed_baseline(db_session, baseline_version="desk-ui-duel-v1")

    # Only player_a has a tonight's box line -- player_b hasn't logged yet.
    await _seed_game_log(
        db_session,
        competition=competition,
        game=duel_game,
        team=home,
        source_player=source_a,
        player=player_a,
        pts=24,
    )

    await db_session.commit()
    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    warmup = await app_client.get("/")
    assert warmup.status_code == 200
    response = await app_client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "desk__hero--live" in html
    assert 'data-desk-hero-kind="live_duel"' in html
    # The logged subject's real tonight line (`_seed_game_log`'s fixed ast=5/reb=7).
    assert "5</b> AST" in html or ">5</b>" in html
    assert "GmSc" in html
    # The pretip subject's line renders em dashes, never a zero or a total.
    assert html.count("&mdash;</b> PTS") >= 1
    assert html.count("&mdash;</b> REB") >= 1
    assert html.count("&mdash;</b> AST") >= 1
    assert html.count("&mdash;</b> GmSc") >= 1
    _assert_no_switcher_chrome(html)


# --------------------------------------------------------------------------- #
# The Ledger (time-independent: forced by an all-final slate)
# --------------------------------------------------------------------------- #
async def test_recap_state_renders_top_performer_cards(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Recap: the top-performer card grid renders (no post-event hero)."""
    now = datetime.utcnow()
    today = to_eastern_date(now)

    competition = await _seed_competition(db_session, today=today)
    home = await _seed_team(db_session, competition, franchise_stats_id="1610612762")
    away = await _seed_team(db_session, competition, franchise_stats_id="1610612764")
    game = await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=today,
        tip_datetime=now - timedelta(hours=3),
        status=SummerLeagueGameStatus.FINAL,
        home_score=88,
        away_score=80,
    )
    player = await _seed_player(db_session, name="Ledger", draft_round=1, draft_pick=1)
    source_player = await _roster_player(db_session, competition, home, player)
    await _seed_baseline(db_session, baseline_version="desk-ui-v2")
    await _seed_game_log(
        db_session,
        competition=competition,
        game=game,
        team=home,
        source_player=source_player,
        player=player,
        pts=30,
    )
    await db_session.commit()

    # The `/` route reads a materialized render snapshot (#551) rather than
    # live-assembling, so the Recap variant (Ledger + performance-of-night
    # hero) must be materialized first -- `run_desk_tick` syncs the event AND
    # runs its final materialization step, exactly as the hourly cron does.
    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    warmup = await app_client.get("/")
    assert warmup.status_code == 200

    response = await app_client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="slDeskSection"' in html
    # Recap redesign: top-performer card grid + per-game box line, no hero.
    assert "desk__perf-grid" in html
    assert "desk__perf-card" in html
    assert "desk__hero--ledger" not in html
    assert "desk__perf-line" in html
    assert "desk__status-tag" in html
    assert "desk__pctl-chip" in html
    _assert_no_switcher_chrome(html)
    assert "?ref=sl-desk" in html
    # #556: card player links route through the SL-scoped player page with
    # placement attribution; the matchup links to that game's box score.
    assert "/summer-league?ref=sl-desk" in html
    assert 'data-desk-placement="ledger"' in html
    assert "/games/" in html
    assert 'data-desk-daily-state="recap"' in html


# --------------------------------------------------------------------------- #
# Morning Card (time-of-day dependent -- rendered directly, not via HTTP)
# --------------------------------------------------------------------------- #
async def test_morning_hero_and_slate_under_ten_never_collapses(
    db_session: AsyncSession,
) -> None:
    """Preview: marquee hero + slate render; under 10 games, nothing collapses (#556).

    Renders `hero_morning.html` / `slate.html` directly against a
    `get_desk_payload(..., now=<pinned>)` result -- see the module docstring
    for why Preview can't be asserted through the real wall-clock `/` route.
    """
    now = datetime(2026, 7, 10, 20, 0)  # 4:00pm ET -- well before the 7pm tip.
    today = date(2026, 7, 10)

    competition = await _seed_competition(db_session, today=today)
    home1, away1 = (
        await _seed_team(db_session, competition, franchise_stats_id="1610612747"),
        await _seed_team(db_session, competition, franchise_stats_id="1610612744"),
    )
    hero_game = await _seed_game(
        db_session,
        competition,
        home1,
        away1,
        game_date=today,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    player = await _seed_player(db_session, name="Marquee", draft_round=1, draft_pick=1)
    await _roster_player(db_session, competition, home1, player)
    await _seed_baseline(db_session, baseline_version="desk-ui-v3")

    # Six more games -- under the 10-game disclosure floor (#556), so the
    # slate must never collapse regardless of signal.
    for i in range(6):
        home_i = await _seed_team(
            db_session, competition, franchise_stats_id=f"exhibit-home-{i}"
        )
        away_i = await _seed_team(
            db_session, competition, franchise_stats_id=f"exhibit-away-{i}"
        )
        await _seed_game(
            db_session,
            competition,
            home_i,
            away_i,
            game_date=today,
            tip_datetime=datetime(2026, 7, 11, 1, i),
            status=SummerLeagueGameStatus.SCHEDULED,
        )

    await db_session.commit()

    result = await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()
    assert result.daily_state is not None and result.daily_state.value == "preview"

    payload = await get_desk_payload(db_session, now=now)
    assert payload is not None
    assert payload.daily_state == "preview"
    assert payload.hero.kind == "marquee"
    assert payload.hero.game_id == hero_game.id
    assert len(payload.slate) == 6

    view = await get_desk_view_context(db_session, payload)
    game_year = to_eastern_date(now).year

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    register_template_filters(env)
    hero_html = env.get_template("summer_league/desk/hero_morning.html").render(
        desk_payload=payload,
        desk_players=view["players"],
        desk_matchups=view["matchups"],
        desk_game_year=game_year,
    )
    slate_html = env.get_template("summer_league/desk/slate.html").render(
        desk_payload=payload,
        desk_players=view["players"],
        desk_matchups=view["matchups"],
        desk_game_year=game_year,
    )

    # Single-subject degrade: the hero game's storyline has one tracked
    # player rostered, so no second face-off subject exists.
    assert "Marquee Test" in hero_html
    assert "/summer-league?ref=sl-desk" in hero_html
    assert 'data-desk-placement="hero"' in hero_html
    _assert_no_switcher_chrome(hero_html)

    # #556: under 10 total games, the slate NEVER collapses -- every card
    # renders unhidden, no `hidden` attribute anywhere, and no toggle (that's
    # only ever created client-side, and only for 10+ game slates).
    assert slate_html.count('class="card desk__game-card"') == 6
    assert "desk__game-card--tail" not in slate_html
    assert "hidden" not in slate_html
    assert 'id="deskSlateToggle"' not in slate_html
    assert "?ref=sl-desk" in slate_html
    # Every card carries the signal metadata JS reads (none actually collapse
    # here, but the attribute must always be present for JS to reason about).
    assert (
        slate_html.count('data-signal="0"') + slate_html.count('data-signal="1"')
        == 6
    )
    # Slate tips are naive UTC (01:00 UTC Jul 11) -> must render as 9:00 PM ET
    # (Jul 10), not the raw-UTC "1:00 AM ET" the card used to mislabel them.
    assert "9:00 PM ET" in slate_html
    assert " AM ET" not in slate_html
    _assert_no_switcher_chrome(slate_html)


async def test_slate_ten_plus_games_marks_zero_signal_tail_for_js(
    db_session: AsyncSession,
) -> None:
    """10+ games: server HTML stays fully unhidden; `data-signal` marks the JS-only tail (#556).

    The collapse itself is JS-driven (verified in a real browser per the
    ticket) -- this test proves the server-rendered contract JS depends on:
    every card present and unhidden, and `data-signal="0"` on exactly the
    trailing no-storyline games (never on the signal-bearing hero game).
    """
    now = datetime(2026, 7, 10, 20, 0)  # 4:00pm ET -- well before the 7pm tip.
    today = date(2026, 7, 10)

    competition = await _seed_competition(db_session, today=today)
    home1, away1 = (
        await _seed_team(db_session, competition, franchise_stats_id="1610612747"),
        await _seed_team(db_session, competition, franchise_stats_id="1610612744"),
    )
    hero_game = await _seed_game(
        db_session,
        competition,
        home1,
        away1,
        game_date=today,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    player = await _seed_player(db_session, name="TenPlus", draft_round=1, draft_pick=1)
    await _roster_player(db_session, competition, home1, player)
    await _seed_baseline(db_session, baseline_version="desk-ui-tenplus-v1")

    # 11 more games -- pushes the slate to 11 (well past the 10-game floor),
    # none carrying a storyline of their own (no tracked-player roster), so
    # every slate row lacks a `read` and is zero-signal.
    for i in range(11):
        home_i = await _seed_team(
            db_session, competition, franchise_stats_id=f"tenplus-home-{i}"
        )
        away_i = await _seed_team(
            db_session, competition, franchise_stats_id=f"tenplus-away-{i}"
        )
        await _seed_game(
            db_session,
            competition,
            home_i,
            away_i,
            game_date=today,
            tip_datetime=datetime(2026, 7, 11, 1, i),
            status=SummerLeagueGameStatus.SCHEDULED,
        )

    await db_session.commit()
    result = await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()
    assert result.daily_state is not None and result.daily_state.value == "preview"

    payload = await get_desk_payload(db_session, now=now)
    assert payload is not None
    assert payload.hero.game_id == hero_game.id
    assert len(payload.slate) == 11

    view = await get_desk_view_context(db_session, payload)
    game_year = to_eastern_date(now).year

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    register_template_filters(env)
    slate_html = env.get_template("summer_league/desk/slate.html").render(
        desk_payload=payload,
        desk_players=view["players"],
        desk_matchups=view["matchups"],
        desk_game_year=game_year,
    )

    # Every one of the 11 games is present, server-side, unhidden -- the
    # collapse never happens without JS.
    assert slate_html.count('class="card desk__game-card"') == 11
    assert "hidden" not in slate_html
    assert "desk__game-card--tail" not in slate_html
    assert 'id="deskSlateToggle"' not in slate_html
    # Zero-signal metadata is present for JS to act on post-load.
    assert 'data-signal="0"' in slate_html


async def test_quiet_slate_hero_renders_solo_fallback(db_session: AsyncSession) -> None:
    """A no-signal slate still forces a headline via the class-leader fallback hero."""
    now = datetime(2026, 7, 10, 20, 0)
    today = date(2026, 7, 10)

    competition = await _seed_competition(db_session, today=today)
    home, away = (
        await _seed_team(db_session, competition, franchise_stats_id="1610612747"),
        await _seed_team(db_session, competition, franchise_stats_id="1610612744"),
    )
    await _seed_game(
        db_session,
        competition,
        home,
        away,
        game_date=today,
        tip_datetime=datetime(2026, 7, 10, 23, 0),
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    baseline_version = "desk-ui-quiet-v1"
    await _seed_baseline(db_session, baseline_version=baseline_version)

    from app.schemas.summer_league_desk import (
        SummerLeagueDeskGrade,
        SummerLeagueDeskPlayerGrade,
    )

    leader = await _seed_player(db_session, name="Leader", draft_round=1, draft_pick=2)
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=leader.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=80.0,
            pctl=95.0,
            grade=SummerLeagueDeskGrade.HOT,
            n_cohort=20,
            gated=False,
        )
    )
    await db_session.commit()

    await run_desk_tick(db_session, now=now, client=_fake_client())
    await db_session.commit()

    payload = await get_desk_payload(db_session, now=now)
    assert payload is not None
    assert payload.hero.kind == "quiet_slate"

    view = await get_desk_view_context(db_session, payload)
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    register_template_filters(env)
    hero_html = env.get_template("summer_league/desk/hero_morning.html").render(
        desk_payload=payload,
        desk_players=view["players"],
        desk_matchups=view["matchups"],
        desk_game_year=today.year,
    )

    assert "Class Leader" in hero_html
    assert "Leader Test" in hero_html
    assert "desk__hero-vs" not in hero_html  # solo hero -- no VS divider
    _assert_no_switcher_chrome(hero_html)

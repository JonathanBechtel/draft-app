"""Integration tests for the Summer League Desk Class Tracker UI (#511).

Covers the ticket's Definition of Done: six cohort toggles (with the
within-round `draft_pick` boundary translated correctly -- see
`app.services.summer_league.cohort_baselines`'s module docstring for the
"draft_pick is WITHIN-ROUND" gotcha this repo hinges on), the Box/Per-36/
Per-100/Advanced stat-view rescale (counting stats scale, shooting
percentages stay invariant, PER/BPM/WS82 em-dash when a pool isn't
`adv_eligible`), the cap-30 + truncation caption, GP=0 em-dashes, the
Undrafted identity swap, `?ref=sl-desk` deep-linking, and the `/` query
budget with cohort/statview params set.

Arithmetic assertions (rescale factors, cohort membership sets) call
`get_desk_payload` directly for precision; HTML-shape assertions (toggle
markup, deep-links, em-dashes) go through the real `/` route, forced into a
time-independent state via an all-`FINAL` slate (Recap -- same technique
`test_sl_desk_ui.py` uses, since Live/Recap are driven by game *status*, not
wall-clock).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrade,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.event_desk.registry import sync_summer_league_event
from app.services.event_desk.render_snapshots import (
    RenderSnapshotWrite,
    upsert_render_snapshots,
)
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.desk_read import (
    TRACKER_CAP,
    build_desk_render_variants,
    get_desk_payload,
)
from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import DESK_HOME_PAGE_BUDGETS

pytestmark = pytest.mark.asyncio

_IDX = {"n": 0}


def _idx() -> int:
    _IDX["n"] += 1
    return _IDX["n"]


async def _materialize_desk_snapshots(db: AsyncSession, *, now: datetime) -> None:
    """Simulate the hourly tick's final step (#551): materialize every render-snapshot variant.

    The `/` route reads a persisted render snapshot (`get_desk_view_from_snapshot`)
    and NEVER live-assembles, so a route render in a test must first materialize
    snapshots exactly as `app/cli/sl_desk_tick.py`'s step 7 does. Uses
    `build_desk_render_variants` (T1-baseline-optional -- degrades gracefully)
    rather than the full `run_desk_tick` so a Tracker test that doesn't seed a
    baseline can still exercise the route. Commits, mirroring the tick's
    caller-owned transaction boundary.
    """
    result = await build_desk_render_variants(db, now=now)
    assert result is not None, "expected an in-window event to materialize"
    event_id, variants = result
    await upsert_render_snapshots(
        db,
        [
            RenderSnapshotWrite(
                event_id=event_id,
                daily_state=v.daily_state,
                tracker_cohort=v.tracker_cohort,
                tracker_stat_view=v.tracker_stat_view,
                view=v.view,
                source_freshness_tick_at=(
                    v.view.payload.freshness.last_tick_at
                    if v.view.payload is not None
                    else None
                ),
                source_freshness_next_tick_eta=(
                    v.view.payload.freshness.next_tick_eta
                    if v.view.payload is not None
                    else None
                ),
            )
            for v in variants
        ],
        now=now,
    )
    await db.commit()


async def _seed_competition(db: AsyncSession, *, year: int) -> SummerLeagueCompetition:
    idx = _idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug=f"vegas-tracker-{idx}",
        display_name=f"{year} Las Vegas",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_team(
    db: AsyncSession, competition: SummerLeagueCompetition, *, franchise_id: str = ""
) -> SummerLeagueTeamEntry:
    idx = _idx()
    assert competition.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=franchise_id or f"tracker-team-{idx}",
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
        team_slug=f"tracker-team-{idx}",
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
    tip_datetime: datetime,
    status: SummerLeagueGameStatus,
) -> SummerLeagueGame:
    idx = _idx()
    assert competition.id is not None and home.id is not None and away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"tracker-game-{idx}",
        game_date=game_date,
        tip_datetime=tip_datetime,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        status=status,
        home_score=70 if status == SummerLeagueGameStatus.FINAL else None,
        away_score=65 if status == SummerLeagueGameStatus.FINAL else None,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return game


async def _seed_player(
    db: AsyncSession,
    *,
    name: str,
    draft_year: int | None,
    draft_round: int | None,
    draft_pick: int | None,
) -> PlayerMaster:
    idx = _idx()
    player = PlayerMaster(
        first_name=name,
        last_name=f"Test{idx}",
        display_name=f"{name} Test{idx}",
        draft_year=draft_year,
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
        nba_stats_person_id=f"tracker-src-{idx}",
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


async def _seed_active_window_game(
    db: AsyncSession, competition: SummerLeagueCompetition, *, now: datetime
) -> None:
    """Seed one game so `sync_summer_league_event` finds an ACTIVE outer window.

    Tests that only need `get_desk_payload`'s tracker section (not the hero/
    slate) still need at least one game on the books -- the outer lifecycle
    resolves from the event's game-date window, which is empty without one.
    """
    home = await _seed_team(db, competition)
    away = await _seed_team(db, competition)
    await _seed_game(
        db,
        competition,
        home,
        away,
        game_date=to_eastern_date(now),
        tip_datetime=now - timedelta(hours=1),
        status=SummerLeagueGameStatus.FINAL,
    )


async def _seed_season(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    player: PlayerMaster,
    year: int,
    gp: int = 3,
    minutes: float = 90.0,
    gmsc: float | None = 20.0,
    pts: int = 60,
    reb: int = 30,
    ast: int = 15,
    stl: int = 6,
    blk: int = 3,
    tov: int = 9,
    fgm: int = 20,
    fga: int = 40,
    fg3m: int = 5,
    fg3a: int = 10,
    ftm: int = 15,
    fta: int = 20,
    pace: float | None = None,
    usg_pct: float | None = None,
    ast_pct: float | None = None,
    tov_pct: float | None = None,
    trb_pct: float | None = None,
    per: float | None = None,
    ws82: float | None = None,
    bpm: float | None = None,
    adv_eligible: bool = False,
) -> SummerLeaguePlayerSeason:
    assert competition.id is not None and player.id is not None
    season = SummerLeaguePlayerSeason(
        competition_id=competition.id,
        player_id=player.id,
        year=year,
        venue_slug=competition.venue_slug,
        is_current=True,
        gp=gp,
        minutes=minutes,
        gmsc=gmsc,
        pts=pts,
        reb=reb,
        ast=ast,
        stl=stl,
        blk=blk,
        tov=tov,
        fgm=fgm,
        fga=fga,
        fg3m=fg3m,
        fg3a=fg3a,
        ftm=ftm,
        fta=fta,
        pace=pace,
        usg_pct=usg_pct,
        ast_pct=ast_pct,
        tov_pct=tov_pct,
        trb_pct=trb_pct,
        per=per,
        ws82=ws82,
        bpm=bpm,
        adv_eligible=adv_eligible,
    )
    db.add(season)
    await db.flush()
    return season


# --------------------------------------------------------------------------- #
# Cohort membership + within-round boundary picks
# --------------------------------------------------------------------------- #
async def test_cohort_membership_including_within_round_boundary_picks(
    db_session: AsyncSession,
) -> None:
    """Boundary overall picks 14/15/30/31 translate correctly to WITHIN-ROUND columns.

    `players_master.draft_pick` is within-round, so an overall pick 31 is
    `draft_round=2, draft_pick=1` (round2's lowest), not `draft_pick=31`.
    """
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)
    competition = await _seed_competition(db_session, year=year)
    team = await _seed_team(db_session, competition)

    # Overall #14 -- lottery's upper boundary.
    lottery_edge = await _seed_player(
        db_session, name="LotteryEdge", draft_year=year, draft_round=1, draft_pick=14
    )
    # Overall #15 -- first round-1 pick NOT in the lottery.
    round1_late = await _seed_player(
        db_session, name="Round1Late", draft_year=year, draft_round=1, draft_pick=15
    )
    # Overall #30 -- round 1's last pick.
    round1_last = await _seed_player(
        db_session, name="Round1Last", draft_year=year, draft_round=1, draft_pick=30
    )
    # Overall #31 -- round 2's first pick (draft_pick=1 WITHIN round 2).
    round2_first = await _seed_player(
        db_session, name="Round2First", draft_year=year, draft_round=2, draft_pick=1
    )
    undrafted = await _seed_player(
        db_session, name="Undrafted", draft_year=None, draft_round=None, draft_pick=None
    )
    sophomore = await _seed_player(
        db_session,
        name="Sophomore",
        draft_year=year - 1,
        draft_round=1,
        draft_pick=3,
    )

    for p in (
        lottery_edge,
        round1_late,
        round1_last,
        round2_first,
        undrafted,
        sophomore,
    ):
        await _roster_player(db_session, competition, team, p)
        await _seed_season(db_session, competition=competition, player=p, year=year)
    await db_session.commit()

    await _seed_active_window_game(db_session, competition, now=now)
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    async def _members(cohort: str) -> set[int]:
        payload = await get_desk_payload(
            db_session, now=now, tracker_cohort=cohort, tracker_stat_view="box"
        )
        assert payload is not None
        return {row.player_id for row in payload.tracker.rows}

    lottery_members = await _members("lottery")
    assert lottery_edge.id in lottery_members
    assert round1_late.id not in lottery_members
    assert round2_first.id not in lottery_members

    round1_members = await _members("round1")
    assert {lottery_edge.id, round1_late.id, round1_last.id} <= round1_members
    assert round2_first.id not in round1_members

    round2_members = await _members("round2")
    assert round2_first.id in round2_members
    assert round1_last.id not in round2_members

    full_class_members = await _members("full_class")
    assert {lottery_edge.id, round1_late.id, round1_last.id, round2_first.id} <= (
        full_class_members
    )
    assert undrafted.id not in full_class_members
    assert sophomore.id not in full_class_members  # prior-year draftee, not this class

    sophomore_members = await _members("sophomores")
    assert sophomore_members == {sophomore.id}

    undrafted_members = await _members("undrafted")
    assert undrafted_members == {undrafted.id}


# --------------------------------------------------------------------------- #
# Sophomores cohort (#543): exactly one class behind (`draft_year == year -
# 1`), not "any earlier draft_year" -- a player drafted two-plus years ago
# must NOT show up as a "prior-year draftee who returned."
# --------------------------------------------------------------------------- #
async def test_sophomores_cohort_admits_prior_year_excludes_older_draftee(
    db_session: AsyncSession,
) -> None:
    """A `year - 1` draftee is a Sophomore; a `year - 2` draftee is not."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)
    competition = await _seed_competition(db_session, year=year)
    team = await _seed_team(db_session, competition)

    prior_year = await _seed_player(
        db_session, name="PriorYear", draft_year=year - 1, draft_round=2, draft_pick=5
    )
    two_years_back = await _seed_player(
        db_session,
        name="TwoYearsBack",
        draft_year=year - 2,
        draft_round=2,
        draft_pick=6,
    )
    for p in (prior_year, two_years_back):
        await _roster_player(db_session, competition, team, p)
        await _seed_season(db_session, competition=competition, player=p, year=year)
    await db_session.commit()

    await _seed_active_window_game(db_session, competition, now=now)
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(
        db_session, now=now, tracker_cohort="sophomores", tracker_stat_view="box"
    )
    assert payload is not None
    sophomore_ids = {row.player_id for row in payload.tracker.rows}

    assert prior_year.id in sophomore_ids
    assert two_years_back.id not in sophomore_ids


# --------------------------------------------------------------------------- #
# Stat-view rescale: Box / Per-36 / Per-100 rescale counting stats; shooting
# percentages stay invariant.
# --------------------------------------------------------------------------- #
async def test_stat_view_rescales_counting_stats_and_keeps_pct_invariant(
    db_session: AsyncSession,
) -> None:
    """PTS rescales exactly by mode; FG%/3P%/FT% are identical across all three."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)
    competition = await _seed_competition(db_session, year=year)
    team = await _seed_team(db_session, competition)

    player = await _seed_player(
        db_session, name="RateCheck", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, team, player)
    # gp=3, minutes=90 (30 MPG); pts=60 (20 PPG); fgm/fga=20/40 (50% FG);
    # fg3m/fg3a=5/10 (50% 3P); ftm/fta=15/20 (75% FT); pace=90.0 (per-48).
    await _seed_season(
        db_session,
        competition=competition,
        player=player,
        year=year,
        gp=3,
        minutes=90.0,
        pts=60,
        fgm=20,
        fga=40,
        fg3m=5,
        fg3a=10,
        ftm=15,
        fta=20,
        pace=90.0,
    )
    await db_session.commit()
    await _seed_active_window_game(db_session, competition, now=now)
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    box = await get_desk_payload(
        db_session, now=now, tracker_cohort="full_class", tracker_stat_view="box"
    )
    per36 = await get_desk_payload(
        db_session, now=now, tracker_cohort="full_class", tracker_stat_view="per36"
    )
    per100 = await get_desk_payload(
        db_session, now=now, tracker_cohort="full_class", tracker_stat_view="per100"
    )
    assert box is not None and per36 is not None and per100 is not None

    box_row = next(r for r in box.tracker.rows if r.player_id == player.id)
    per36_row = next(r for r in per36.tracker.rows if r.player_id == player.id)
    per100_row = next(r for r in per100.tracker.rows if r.player_id == player.id)

    # Box (per-game): 60 pts / 3 gp = 20.0 PPG.
    assert box_row.stat_columns["pts"] == pytest.approx(20.0)
    # Per-36: PTS x 36 / total MIN = 60 * 36 / 90 = 24.0.
    assert per36_row.stat_columns["pts"] == pytest.approx(24.0)
    # Per-100: pace=90 (per-48) over 90 minutes -> poss = (90*90/48) * (90/90)
    # = 168.75; PTS x 100 / poss = 60 * 100 / 168.75 ~= 35.6.
    assert per100_row.stat_columns["pts"] == pytest.approx(35.6, abs=0.05)

    # Shooting percentages are recombined from pooled makes/attempts -- they
    # do not change across Box/Per-36/Per-100 (only counting stats rescale).
    for row in (box_row, per36_row, per100_row):
        assert row.stat_columns["fg_pct"] == pytest.approx(50.0)
        assert row.stat_columns["fg3_pct"] == pytest.approx(50.0)
        assert row.stat_columns["ft_pct"] == pytest.approx(75.0)


# --------------------------------------------------------------------------- #
# Advanced view: PER/BPM/WS82 em-dash (None) when the pool isn't adv_eligible;
# real values when it is.
# --------------------------------------------------------------------------- #
async def test_advanced_view_bpm_null_when_not_adv_eligible(
    db_session: AsyncSession,
) -> None:
    """PER/BPM/WS82 render `None` (em-dash) for a non-adv-eligible pool; real otherwise."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)
    competition = await _seed_competition(db_session, year=year)
    team = await _seed_team(db_session, competition)

    ineligible = await _seed_player(
        db_session, name="Thin", draft_year=year, draft_round=1, draft_pick=2
    )
    eligible = await _seed_player(
        db_session, name="Calibrated", draft_year=year, draft_round=1, draft_pick=3
    )
    await _roster_player(db_session, competition, team, ineligible)
    await _roster_player(db_session, competition, team, eligible)

    # Ineligible pool: per/bpm/ws82/usg_pct etc. are None, matching how
    # `app.services.summer_league.metrics` writes a non-adv_eligible pool.
    await _seed_season(
        db_session,
        competition=competition,
        player=ineligible,
        year=year,
        per=None,
        bpm=None,
        ws82=None,
        usg_pct=None,
        adv_eligible=False,
    )
    await _seed_season(
        db_session,
        competition=competition,
        player=eligible,
        year=year,
        per=18.4,
        bpm=4.2,
        ws82=6.5,
        usg_pct=24.0,
        ast_pct=18.0,
        tov_pct=11.0,
        trb_pct=9.0,
        adv_eligible=True,
    )
    await db_session.commit()
    await _seed_active_window_game(db_session, competition, now=now)
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(
        db_session, now=now, tracker_cohort="full_class", tracker_stat_view="advanced"
    )
    assert payload is not None

    ineligible_row = next(
        r for r in payload.tracker.rows if r.player_id == ineligible.id
    )
    eligible_row = next(r for r in payload.tracker.rows if r.player_id == eligible.id)

    assert ineligible_row.stat_columns["per"] is None
    assert ineligible_row.stat_columns["bpm"] is None
    assert ineligible_row.stat_columns["ws82"] is None

    assert eligible_row.stat_columns["per"] == pytest.approx(18.4)
    assert eligible_row.stat_columns["bpm"] == pytest.approx(4.2)
    assert eligible_row.stat_columns["ws82"] == pytest.approx(6.5)
    assert eligible_row.stat_columns["usg_pct"] == pytest.approx(24.0)


# --------------------------------------------------------------------------- #
# Cap-30 + truncation flag
# --------------------------------------------------------------------------- #
async def test_cohort_over_30_caps_at_30_by_gmsc_and_flags_truncated(
    db_session: AsyncSession,
) -> None:
    """A 35-member cohort renders the top 30 by GmSc; `truncated` is True."""
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)
    competition = await _seed_competition(db_session, year=year)
    team = await _seed_team(db_session, competition)

    n = TRACKER_CAP + 5
    players: list[PlayerMaster] = []
    for i in range(n):
        p = await _seed_player(
            db_session, name=f"Round2P{i}", draft_year=year, draft_round=2, draft_pick=1
        )
        await _roster_player(db_session, competition, team, p)
        # Descending GmSc so the top-30 slice is deterministic.
        await _seed_season(
            db_session,
            competition=competition,
            player=p,
            year=year,
            gmsc=float(n - i),
        )
        players.append(p)
    await db_session.commit()
    await _seed_active_window_game(db_session, competition, now=now)
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(
        db_session, now=now, tracker_cohort="round2", tracker_stat_view="box"
    )
    assert payload is not None
    assert len(payload.tracker.rows) == TRACKER_CAP
    assert payload.tracker.truncated is True
    top_30_ids = {p.id for p in players[:TRACKER_CAP]}
    assert {r.player_id for r in payload.tracker.rows} == top_30_ids

    # A cohort under the cap is NOT truncated.
    lottery_payload = await get_desk_payload(
        db_session, now=now, tracker_cohort="lottery", tracker_stat_view="box"
    )
    assert lottery_payload is not None
    assert lottery_payload.tracker.truncated is False


# --------------------------------------------------------------------------- #
# HTML-shape assertions: GP=0 em-dashes, Undrafted identity swap, ref-tagging,
# toggle markup -- driven through the real `/` route, forced to Recap (a
# time-independent state -- Live/Recap resolve from game *status*, not the
# wall clock) via an all-FINAL slate.
# --------------------------------------------------------------------------- #
async def _seed_recap_window(
    db: AsyncSession, *, now: datetime
) -> SummerLeagueCompetition:
    """An active SL event with an all-FINAL slate -- forces Recap deterministically."""
    today = to_eastern_date(now)
    competition = await _seed_competition(db, year=today.year)
    home = await _seed_team(db, competition)
    away = await _seed_team(db, competition)
    await _seed_game(
        db,
        competition,
        home,
        away,
        game_date=today,
        tip_datetime=now - timedelta(hours=3),
        status=SummerLeagueGameStatus.FINAL,
    )
    return competition


async def test_gp_zero_row_renders_em_dashes(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A rostered player with no season row (GP=0, e.g. debuts tonight) shows em-dashes."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    debut = await _seed_player(
        db_session, name="Debuting", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, team, debut)
    # No SummerLeaguePlayerSeason row seeded for `debut` -- GP=0 case.
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=full_class&statview=box")
    assert warmup.status_code == 200
    response = await app_client.get("/?cohort=full_class&statview=box")
    assert response.status_code == 200
    html = response.text

    assert "slDeskTracker" in html
    assert "Debuting Test" in html
    assert "&mdash;" in html


async def test_undrafted_identity_swap_and_status_cohort_label(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The Undrafted cohort swaps identity to status form and relabels 'vs status cohort'."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition, franchise_id="1610612747")

    undrafted = await _seed_player(
        db_session, name="Grinder", draft_year=None, draft_round=None, draft_pick=None
    )
    await _roster_player(db_session, competition, team, undrafted)
    await _seed_season(db_session, competition=competition, player=undrafted, year=year)
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=undrafted&statview=box")
    assert warmup.status_code == 200
    response = await app_client.get("/?cohort=undrafted&statview=box")
    assert response.status_code == 200
    html = response.text

    assert "Grinder Test" in html
    assert "Undrafted &middot;" in html or "Undrafted ·" in html
    assert "vs status cohort" in html
    assert "?ref=sl-desk" in html


async def test_cohort_and_statview_toggles_round_trip_via_query_params(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Toggle links carry BOTH params; the active toggle + column set match the request."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    player = await _seed_player(
        db_session, name="Toggle", draft_year=year, draft_round=2, draft_pick=5
    )
    await _roster_player(db_session, competition, team, player)
    await _seed_season(db_session, competition=competition, player=player, year=year)
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=round2&statview=advanced")
    assert warmup.status_code == 200
    response = await app_client.get("/?cohort=round2&statview=advanced")
    assert response.status_code == 200
    html = response.text

    # Advanced column headers present (now carrying #556's sort attributes,
    # so match by inner text rather than the exact old bare `<th>` tag);
    # box-family headers are not.
    assert 'data-sort-key="per"' in html
    assert '>PER</th>' in html
    assert 'data-sort-key="bpm">BPM</th>' in html
    assert 'data-sort-key="ws82">WS/82</th>' in html
    assert ">PTS</th>" not in html

    # Cohort toggle link for the currently-inactive "lottery" preserves the
    # active statview; the active cohort/statview render with `is-active`.
    assert 'href="/?cohort=lottery&statview=advanced#slDeskTracker"' in html
    assert 'class="desk__tracker-toggle-btn is-active"' in html
    assert 'class="slg-mode-btn is-active"' in html

    # #567: each toggle carries the data attribute `initTrackerToggles` reads
    # to resolve the OTHER axis and build the fetch URL client-side.
    assert 'data-cohort="lottery"' in html
    assert 'data-statview="advanced"' in html


# --------------------------------------------------------------------------- #
# Class Tracker JS fetch-and-swap fragment route (#567): `/desk/tracker`
# lets `summer-league-desk.js`'s `initTrackerToggles` switch tabs without a
# full `/` navigation. It reads the identical snapshot row `/` does for the
# same (cohort, statview) and renders ONLY `class_tracker_table.html` -- no
# page chrome, no consensus/news/hero markup.
# --------------------------------------------------------------------------- #
async def test_tracker_fragment_route_matches_full_page_for_same_variant(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`/desk/tracker?cohort=..&statview=..` renders the SAME table `/` does, chrome-free."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    player = await _seed_player(
        db_session, name="Fragment", draft_year=year, draft_round=2, draft_pick=5
    )
    await _roster_player(db_session, competition, team, player)
    await _seed_season(db_session, competition=competition, player=player, year=year)
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    full_page = await app_client.get("/?cohort=round2&statview=advanced")
    assert full_page.status_code == 200

    fragment = await app_client.get("/desk/tracker?cohort=round2&statview=advanced")
    assert fragment.status_code == 200
    html = fragment.text

    # Same variant's column set and row present.
    assert 'data-sort-key="per"' in html
    assert '>PER</th>' in html
    assert 'data-sort-key="bpm">BPM</th>' in html
    assert 'data-sort-key="ws82">WS/82</th>' in html
    assert ">PTS</th>" not in html
    assert "Fragment Test" in html

    # No page chrome -- this is the table/caption fragment ONLY, never the
    # toggle bar (that stays static across a client-side swap) or anything
    # from outside the Desk.
    assert "<html" not in html
    assert 'id="slDeskTracker"' not in html
    assert "desk__tracker-toggle-btn" not in html
    assert "consensus-hero" not in html


async def test_tracker_fragment_route_off_window_renders_empty_state(
    app_client: AsyncClient,
) -> None:
    """No active SL event (or unmaterialized variant): fragment degrades to empty, not 500."""
    response = await app_client.get("/desk/tracker?cohort=lottery&statview=box")
    assert response.status_code == 200
    assert "No tracked players in this cohort yet." in response.text
    assert "<html" not in response.text


async def test_tracker_row_deep_link_carries_ref_sl_desk(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A player row's link to their SL page carries `?ref=sl-desk`."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    player = await _seed_player(
        db_session, name="DeepLink", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, team, player)
    await _seed_season(db_session, competition=competition, player=player, year=year)
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/")
    assert warmup.status_code == 200
    response = await app_client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "?ref=sl-desk" in html
    # #556: Desk player links route through the SL-scoped player page, not
    # the generic player page, and carry placement attribution for the
    # `sl_desk_click` analytics event.
    assert "/summer-league?ref=sl-desk" in html
    assert 'data-desk-placement="tracker"' in html


# --------------------------------------------------------------------------- #
# Client-side column sort (#556): sortable numeric headers carry
# `data-sort-key`; rows carry matching `data-value` (empty for `None`) and a
# `data-name` tiebreak. The reorder itself is pure client-side JS (verified
# in a real browser per the ticket) -- these assertions cover the rendered
# metadata contract JS depends on.
# --------------------------------------------------------------------------- #
async def test_tracker_sort_metadata_rendered_box_view(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Box view: numeric headers carry `data-sort-key`; GP=0 row's MIN sorts as missing."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    logged = await _seed_player(
        db_session, name="Logged", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, team, logged)
    await _seed_season(db_session, competition=competition, player=logged, year=year)

    debut = await _seed_player(
        db_session, name="Debut", draft_year=year, draft_round=1, draft_pick=2
    )
    await _roster_player(db_session, competition, team, debut)
    # No season row -- GP=0, MIN must render as a missing (empty data-value).
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=lottery&statview=box")
    assert warmup.status_code == 200
    response = await app_client.get("/?cohort=lottery&statview=box")
    assert response.status_code == 200
    html = response.text

    # Sortable numeric headers.
    for key in ("gp", "minutes", "gmsc", "pts", "reb", "ast", "stl", "blk", "tov"):
        assert f'data-sort-key="{key}"' in html, f"missing sort header for {key}"
    # GmSc defaults to the pre-sorted descending state.
    assert 'data-sort-key="gmsc" aria-sort="descending"' in html
    # A GP=0 row's MIN cell has no data-value (missing -> sorts last).
    assert 'data-value=""' in html
    # The logged row's name is present for the stable-tiebreak attribute.
    assert 'data-name="Logged Test' in html


async def test_tracker_sort_metadata_rendered_advanced_view(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Advanced view: PER/BPM/WS82 sort keys render; a None stat has an empty data-value."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    ineligible = await _seed_player(
        db_session, name="Ineligible", draft_year=year, draft_round=1, draft_pick=1
    )
    await _roster_player(db_session, competition, team, ineligible)
    await _seed_season(
        db_session,
        competition=competition,
        player=ineligible,
        year=year,
        per=None,
        bpm=None,
        ws82=None,
        adv_eligible=False,
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=lottery&statview=advanced")
    assert warmup.status_code == 200
    response = await app_client.get("/?cohort=lottery&statview=advanced")
    assert response.status_code == 200
    html = response.text

    for key in (
        "per",
        "ts_pct",
        "efg_pct",
        "usg_pct",
        "ast_pct",
        "tov_pct",
        "trb_pct",
        "fg3ar",
        "ftr",
        "ws82",
        "bpm",
    ):
        assert f'data-sort-key="{key}"' in html, f"missing sort header for {key}"
    # The ineligible player's PER/BPM/WS82 are None -> empty data-value.
    assert 'data-value=""' in html


# --------------------------------------------------------------------------- #
# T2 grade contract (#543): the Tracker's `grade` column READS the persisted
# `summer_league_desk_player_grades` row (value + gated state) rather than
# recomputing a percentile from T1 breakpoints at request time. A gated row
# (e.g. a one-game sample the adaptive gate ladder flagged as not-yet-
# confident) renders exactly like an ungraded player -- `grade=None`, the
# template's em-dash -- never a fabricated Hot/Warm/Cold label.
# --------------------------------------------------------------------------- #
async def _seed_t1_baseline(
    db: AsyncSession, *, baseline_version: str, cohort_key: str = "slot:1-4"
) -> None:
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key=cohort_key,
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=SummerLeagueDeskGrain.EVENT,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=40.0,
            n_members=20,
            breakpoints={"0": 10.0, "50": 25.0, "100": 40.0},
            mean_value=25.0,
            median_value=25.0,
        )
    )


async def test_qualified_row_retains_persisted_grade_gated_row_grade_is_none(
    db_session: AsyncSession,
) -> None:
    """A confident T2 row keeps its persisted grade; a gated one T2 row grades `None`.

    Arithmetic/data-shape assertion via `get_desk_payload` directly (per this
    module's own convention -- see file docstring) rather than through HTML,
    for precision on the exact persisted value read back.
    """
    year = 2026
    now = datetime(2026, 7, 10, 20, 0)
    competition = await _seed_competition(db_session, year=year)
    team = await _seed_team(db_session, competition)

    qualified = await _seed_player(
        db_session, name="Qualified", draft_year=year, draft_round=1, draft_pick=1
    )
    one_game = await _seed_player(
        db_session, name="OneGame", draft_year=year, draft_round=1, draft_pick=1
    )
    for p in (qualified, one_game):
        await _roster_player(db_session, competition, team, p)
        await _seed_season(
            db_session, competition=competition, player=p, year=year, gp=1, gmsc=25.0
        )
    await db_session.commit()

    baseline_version = "tracker-t2-v1"
    await _seed_t1_baseline(db_session, baseline_version=baseline_version)
    assert (
        qualified.id is not None
        and one_game.id is not None
        and competition.id is not None
    )
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=qualified.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=25.0,
            pctl=70.0,
            grade=SummerLeagueDeskGrade.WARM,
            n_cohort=20,
            gated=False,
        )
    )
    # A one-game sample: the tick still computed a (would-be HOT) percentile,
    # but the adaptive gate ladder flagged it as not-yet-confident -- exactly
    # the case the Tracker must render as unqualified, not as "Hot".
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=one_game.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=39.0,
            pctl=98.0,
            grade=SummerLeagueDeskGrade.HOT,
            n_cohort=20,
            gated=True,
        )
    )
    await db_session.commit()

    await _seed_active_window_game(db_session, competition, now=now)
    await sync_summer_league_event(db_session, now.date())
    await db_session.commit()

    payload = await get_desk_payload(
        db_session, now=now, tracker_cohort="lottery", tracker_stat_view="box"
    )
    assert payload is not None
    rows_by_id = {r.player_id: r for r in payload.tracker.rows}

    assert rows_by_id[qualified.id].grade == "warm"
    assert rows_by_id[one_game.id].grade is None


async def test_gated_row_renders_unqualified_qualified_row_shows_grade_chip(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """HTML-shape: the gated row's grade cell renders the em-dash, never a chip."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition)

    qualified = await _seed_player(
        db_session, name="Confident", draft_year=year, draft_round=1, draft_pick=1
    )
    one_game = await _seed_player(
        db_session, name="Thin", draft_year=year, draft_round=1, draft_pick=1
    )
    for p in (qualified, one_game):
        await _roster_player(db_session, competition, team, p)
        await _seed_season(
            db_session, competition=competition, player=p, year=year, gp=1, gmsc=25.0
        )
    await db_session.commit()

    baseline_version = "tracker-t2-v2"
    await _seed_t1_baseline(db_session, baseline_version=baseline_version)
    assert (
        qualified.id is not None
        and one_game.id is not None
        and competition.id is not None
    )
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=qualified.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=25.0,
            pctl=70.0,
            grade=SummerLeagueDeskGrade.WARM,
            n_cohort=20,
            gated=False,
        )
    )
    db_session.add(
        SummerLeagueDeskPlayerGrade(
            player_id=one_game.id,
            competition_id=competition.id,
            baseline_version=baseline_version,
            cohort_key="slot:1-4",
            subject_value=39.0,
            pctl=98.0,
            grade=SummerLeagueDeskGrade.HOT,
            n_cohort=20,
            gated=True,
        )
    )
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=lottery&statview=box")
    assert warmup.status_code == 200
    response = await app_client.get("/?cohort=lottery&statview=box")
    assert response.status_code == 200
    html = response.text

    assert "Confident Test" in html
    assert "Thin Test" in html
    # The qualified row's chip renders its persisted grade.
    assert 'desk__pctl-chip--warm">Warm</span>' in html
    # The gated row never renders a chip of any grade -- HOT never appears,
    # even though the tick computed a (gated, would-be-HOT) percentile.
    assert "desk__pctl-chip--hot" not in html


# --------------------------------------------------------------------------- #
# Query budget with cohort/statview params set (ticket DoD).
# --------------------------------------------------------------------------- #
async def test_query_budget_holds_with_tracker_params(
    app_client: AsyncClient, db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """`/` with `?cohort=...&statview=...` set stays within `DESK_HOME_PAGE_BUDGETS["recap"]`."""
    now = datetime.utcnow()
    today = to_eastern_date(now)
    year = today.year
    competition = await _seed_recap_window(db_session, now=now)
    team = await _seed_team(db_session, competition, franchise_id="1610612747")

    for i in range(3):
        p = await _seed_player(
            db_session,
            name=f"Budget{i}",
            draft_year=year,
            draft_round=1,
            draft_pick=i + 1,
        )
        await _roster_player(db_session, competition, team, p)
        await _seed_season(db_session, competition=competition, player=p, year=year)
    await db_session.commit()
    await sync_summer_league_event(db_session, today)
    await db_session.commit()
    await _materialize_desk_snapshots(db_session, now=now)

    warmup = await app_client.get("/?cohort=lottery&statview=advanced")
    assert warmup.status_code == 200

    with count_queries(async_engine) as captured:
        response = await app_client.get("/?cohort=lottery&statview=advanced")
    assert response.status_code == 200

    budget = DESK_HOME_PAGE_BUDGETS["recap"]
    assert len(captured) <= budget, (
        f"/ with tracker params issued {len(captured)} queries, over budget of "
        f"{budget}: {captured}"
    )

"""Integration tests for the Summer League advanced-metrics read service + route.

Exercises :func:`get_player_metric_seasons` against the materialized
``summer_league_player_seasons`` table and the player-detail page:

- Non-eligible-only players yield ``None`` (advanced view omitted).
- Eligible competitions are returned newest-first, Las Vegas first within a year.
- The career line sums WS/VORP and minute-weights PER/BPM.
- The player page renders the "Advanced Metrics" table when (and only when) the
  player has an adv-eligible competition.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league_metrics_service import get_player_metric_seasons
from tests.integration.conftest import make_player

_SEQ = {"n": 0}


async def _competition(
    db: AsyncSession, *, year: int, venue_slug: str, league_id: str
) -> SummerLeagueEdition:
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
    )
    db.add(comp)
    await db.flush()
    return comp


async def _player(db: AsyncSession, first: str, last: str):
    player = make_player(first, last)
    db.add(player)
    await db.flush()
    return player


async def _season(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    player,
    adv_eligible: bool = True,
    **metrics,
) -> SummerLeaguePlayerSeason:
    row = SummerLeaguePlayerSeason(
        competition_id=comp.id,
        player_id=player.id,
        year=comp.year,
        venue_slug=comp.venue_slug,
        is_current=True,
        gp=metrics.pop("gp", 3),
        minutes=metrics.pop("minutes", 90.0),
        adv_eligible=adv_eligible,
        **metrics,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_non_eligible_only_returns_none(db_session: AsyncSession) -> None:
    """A player with only a non-eligible competition has no advanced profile."""
    comp = await _competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="15"
    )
    player = await _player(db_session, "Thin", "Pool")
    await _season(db_session, comp=comp, player=player, adv_eligible=False)
    await db_session.flush()

    assert player.id is not None
    profile = await get_player_metric_seasons(db_session, player.id)
    assert profile is None


@pytest.mark.asyncio
async def test_eligible_seasons_sorted_and_filtered(db_session: AsyncSession) -> None:
    """Only eligible comps returned; newest year first, Vegas before warm-ups."""
    player = await _player(db_session, "Sorted", "Seasons")

    cc_2025 = await _competition(
        db_session, year=2025, venue_slug="california_classic", league_id="13"
    )
    lv_2025 = await _competition(
        db_session, year=2025, venue_slug="las_vegas", league_id="15"
    )
    lv_2024 = await _competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="15"
    )
    thin_2023 = await _competition(
        db_session, year=2023, venue_slug="orlando", league_id="14"
    )

    await _season(db_session, comp=cc_2025, player=player, per=15.0)
    await _season(db_session, comp=lv_2025, player=player, per=20.0)
    await _season(db_session, comp=lv_2024, player=player, per=18.0)
    # Non-eligible: must be filtered out entirely.
    await _season(db_session, comp=thin_2023, player=player, adv_eligible=False)
    await db_session.flush()

    assert player.id is not None
    profile = await get_player_metric_seasons(db_session, player.id)
    assert profile is not None
    order = [(s.year, s.venue_slug) for s in profile.seasons]
    assert order == [
        (2025, "las_vegas"),
        (2025, "california_classic"),
        (2024, "las_vegas"),
    ]


@pytest.mark.asyncio
async def test_career_rollup_sums_and_weights(db_session: AsyncSession) -> None:
    """Career WS/VORP are summed; PER/BPM minute-weighted across eligible comps."""
    player = await _player(db_session, "Career", "Rollup")
    lv = await _competition(
        db_session, year=2025, venue_slug="las_vegas", league_id="15"
    )
    slc = await _competition(
        db_session, year=2025, venue_slug="salt_lake_city", league_id="16"
    )

    await _season(
        db_session,
        comp=lv,
        player=player,
        minutes=100.0,
        per=18.0,
        bpm=4.0,
        ws=1.0,
        vorp=0.6,
        ws82=8.0,
        vorp82=6.0,
    )
    await _season(
        db_session,
        comp=slc,
        player=player,
        minutes=300.0,
        per=22.0,
        bpm=8.0,
        ws=2.0,
        vorp=1.4,
        ws82=12.0,
        vorp82=10.0,
    )
    await db_session.flush()

    assert player.id is not None
    profile = await get_player_metric_seasons(db_session, player.id)
    assert profile is not None
    career = profile.career
    assert career.minutes == 400.0
    assert career.ws == 3.0  # cumulative, summed
    assert career.vorp == 2.0  # cumulative, summed
    assert career.per_avg == 21.0  # (18*100 + 22*300)/400
    assert career.bpm_avg == 7.0  # (4*100 + 8*300)/400
    assert career.ws82_avg == 11.0  # (8*100 + 12*300)/400, minute-weighted
    assert career.vorp82_avg == 9.0  # (6*100 + 10*300)/400, minute-weighted


@pytest.mark.asyncio
async def test_sub_threshold_minutes_excluded(db_session: AsyncSession) -> None:
    """A thin (<40 min) competition is hidden so small-sample blowups don't show."""
    player = await _player(db_session, "Cup", "Coffee")
    comp = await _competition(
        db_session, year=2025, venue_slug="las_vegas", league_id="15"
    )
    # Adv-eligible pool, but the player logged only 12 minutes — PER would be noise.
    await _season(db_session, comp=comp, player=player, minutes=12.0, gp=1, per=70.0)
    await db_session.flush()

    assert player.id is not None
    assert await get_player_metric_seasons(db_session, player.id) is None


@pytest.mark.asyncio
async def test_percentage_columns_not_rescaled(db_session: AsyncSession) -> None:
    """Stored percentages (e.g. TS% 60.6) pass through as-is, never ×100."""
    player = await _player(db_session, "Pct", "Scale")
    comp = await _competition(
        db_session, year=2025, venue_slug="las_vegas", league_id="15"
    )
    await _season(
        db_session, comp=comp, player=player, ts_pct=60.6, usg_pct=22.3, per=19.1
    )
    await db_session.flush()

    assert player.id is not None
    profile = await get_player_metric_seasons(db_session, player.id)
    assert profile is not None
    season = profile.seasons[0]
    assert season.ts_pct == 60.6
    assert season.usg_pct == 22.3


async def _seed_minimal_game_log(
    db: AsyncSession, *, comp: SummerLeagueEdition, player
) -> None:
    """Seed one game log so the player-detail SL section renders for the page."""
    _SEQ["n"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"team-{_SEQ['n']}",
        raw_team_name="Home",
        raw_team_abbreviation="HOM",
        team_slug=f"hom-{_SEQ['n']}",
    )
    opp = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"team-{_SEQ['n']}-o",
        raw_team_name="Away",
        raw_team_abbreviation="AWY",
        team_slug=f"awy-{_SEQ['n']}",
    )
    db.add_all([team, opp])
    await db.flush()
    source = SummerLeagueSourceRecord(
        nba_stats_person_id=f"sp-{_SEQ['n']}",
        raw_player_name=player.display_name or "P",
        normalized_name=(player.display_name or "p").lower(),
        canonical_player_id=player.id,
    )
    db.add(source)
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"game-{_SEQ['n']}",
        game_date=date(comp.year, 7, 12),
        home_team_entry_id=team.id,
        away_team_entry_id=opp.id,
        home_score=100,
        away_score=90,
    )
    db.add(game)
    await db.flush()
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=source.id,
            player_id=player.id,
            nba_stats_person_id=source.nba_stats_person_id,
            raw_player_name=player.display_name or "Player",
            minutes_seconds=1800,
            pace=100.0,
            pts=20,
            reb=10,
            ast=4,
            fgm=8,
            fga=15,
            fg3m=1,
            fg3a=3,
            ftm=3,
            fta=4,
            stl=1,
            blk=0,
            tov=2,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_player_page_renders_advanced_table(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """An adv-eligible season surfaces the Advanced Metrics table on the page."""
    player = await _player(db_session, "Advanced", "Star")
    comp = await _competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="15"
    )
    await _seed_minimal_game_log(db_session, comp=comp, player=player)
    await _season(
        db_session,
        comp=comp,
        player=player,
        per=24.7,
        ws=2.3,
        vorp=1.1,
        ftr=0.42,
        ast_pct=25.3,
        tov_pct=11.2,
        orb_pct=9.4,
        stl_pct=2.6,
        ows=1.8,
        obpm=3.9,
        ast_fgm=7,
        unast_fgm=3,
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    html = resp.text
    assert "Advanced Metrics" in html
    assert "24.7" in html  # PER value rendered
    # Full BBRef-parity advanced header set renders with stored values.
    for header in (
        ">3PAr<",
        ">FTr<",
        ">AST'd%<",
        ">ORB%<",
        ">DRB%<",
        ">TRB%<",
        ">AST%<",
        ">STL%<",
        ">BLK%<",
        ">TOV%<",
        ">USG%<",
        ">OBPM<",
        ">DBPM<",
        ">OWS<",
        ">DWS<",
        ">WS/40<",
    ):
        assert header in html
    assert "0.420" in html  # FTr as a 3-decimal fraction
    assert "25.3" in html
    assert "11.2" in html
    assert "9.4" in html  # ORB%
    assert "70.0" in html  # AST'd% = 100*7/10
    assert "3.9" in html  # OBPM


@pytest.mark.asyncio
async def test_player_page_omits_advanced_without_eligible(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A player with logs but no adv-eligible season shows no Advanced table."""
    player = await _player(db_session, "NoAdv", "Player")
    comp = await _competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="15"
    )
    await _seed_minimal_game_log(db_session, comp=comp, player=player)
    await _season(db_session, comp=comp, player=player, adv_eligible=False)
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    assert "summerLeagueSection" in resp.text  # base SL section still renders
    assert "Advanced Metrics" not in resp.text

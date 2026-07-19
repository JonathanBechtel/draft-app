"""Integration tests for Competition Context reuse on season/venue (#610).

Covers the frozen implementation-contract §7 public-surface-reuse rules as
they apply to the season hub and venue page:

* the season hub keeps its venue/event portfolio cards *and* adds one
  explicitly labeled all-competitions summary;
* the venue page renders exactly one competition profile, resolved by the
  page's canonical ``competition_id`` — never by venue label alone;
* a missing profile renders a neutral unavailable state, never an error;
* two competitions that share a venue slug across different years can never
  leak one profile's values into the other's page (the collision case the
  ticket requires).
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.summer_league_environment_service import (
    competition_scope_key,
    rebuild_environment_profiles,
    season_scope_key,
)
from tests.integration.conftest import make_player

pytestmark = pytest.mark.asyncio

_N = {"i": 0}


def _team_line(*, pts: int, fga: int, fg3a: int, tov: int) -> dict:
    """A regulation-minute team box line.

    Only the passed fields vary between competitions so two profiles are
    provably distinct.
    """
    return dict(
        minutes=200,
        pts=pts,
        fgm=int(fga * 0.45),
        fga=fga,
        fg3m=int(fg3a * 0.35),
        fg3a=fg3a,
        ftm=14,
        fta=18,
        oreb=10,
        dreb=32,
        reb=42,
        ast=20,
        stl=7,
        blk=4,
        tov=tov,
        pf=18,
    )


async def _competition(
    db: AsyncSession, *, year: int, venue_slug: str, league_id: str
) -> SummerLeagueCompetition:
    _N["i"] += 1
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 8),
        ends_on=date(year, 7, 18),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _team(db: AsyncSession, comp_id: int) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"team-{_N['i']}",
        raw_team_name=f"Team {_N['i']}",
        team_slug=f"team-{_N['i']}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _seed_box_complete_competition(
    db: AsyncSession,
    *,
    year: int,
    venue_slug: str,
    league_id: str,
    n_games: int = 2,
    line_a: dict | None = None,
    line_b: dict | None = None,
) -> int:
    """Seed a two-team competition with ``n_games`` box-complete final games.

    Also seeds one resolved appeared player so field-composition metrics have
    a non-zero denominator. Returns the competition id.
    """
    line_a = line_a or _team_line(pts=110, fga=88, fg3a=34, tov=13)
    line_b = line_b or _team_line(pts=98, fga=90, fg3a=28, tov=16)

    comp = await _competition(db, year=year, venue_slug=venue_slug, league_id=league_id)
    assert comp.id is not None
    team_a = await _team(db, comp.id)
    team_b = await _team(db, comp.id)
    assert team_a.id is not None and team_b.id is not None

    player = make_player(f"Player{_N['i']}", "Test")
    db.add(player)
    await db.flush()
    _N["i"] += 1
    source = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"sp-{_N['i']}",
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source)
    await db.flush()

    for g in range(n_games):
        _N["i"] += 1
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"g-{_N['i']}",
            game_date=date(year, 7, 8 + g),
            home_team_entry_id=team_a.id,
            away_team_entry_id=team_b.id,
            home_score=line_a["pts"],
            away_score=line_b["pts"],
        )
        db.add(game)
        await db.flush()
        assert game.id is not None
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=team_a.id,
                **line_a,
            )
        )
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=team_b.id,
                **line_b,
            )
        )
        db.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=team_a.id,
                source_player_id=source.id,
                player_id=player.id,
                nba_stats_person_id=source.nba_stats_person_id,
                raw_player_name=source.raw_player_name,
                minutes_seconds=1500,
                pts=12,
            )
        )
    await db.flush()
    return comp.id


# --------------------------------------------------------------------------- #
# Season hub: all-competitions summary coexists with venue cards
# --------------------------------------------------------------------------- #
async def test_season_hub_shows_all_competitions_summary_alongside_venue_cards(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The season hub keeps venue cards and adds a labeled season:<year> summary."""
    comp_a = await _seed_box_complete_competition(
        db_session, year=2025, venue_slug="las_vegas", league_id="15"
    )
    comp_b = await _seed_box_complete_competition(
        db_session, year=2025, venue_slug="salt_lake_city", league_id="13"
    )
    await db_session.commit()

    async with db_session.begin():
        result = await rebuild_environment_profiles(db_session, year=2025)
    assert result.failed_scopes == 0

    resp = await app_client.get("/stats/summer-league/2025")
    assert resp.status_code == 200
    html = resp.text

    # Existing venue portfolio cards remain.
    assert "Las Vegas" in html and "Salt Lake City" in html
    assert "/stats/summer-league/2025/las_vegas" in html

    # New, explicitly labeled all-competitions summary.
    assert "All-Competitions Summary" in html
    assert "All competitions" in html
    assert season_scope_key(2025) in html  # "season:2025" stable scope key
    # The Explorer link (query params HTML-entity-escaped by Jinja autoescape).
    assert "/stats/summer-league/explorer?subject=competitions" in html
    assert "profile_scope=season" in html
    assert "detail_year=2025" in html
    # A headline metric renders with a real (non-em-dash) value.
    assert "Pace (per 48)" in html
    # The summary never claims to be a single competition's id.
    assert competition_scope_key(comp_a) not in html
    assert competition_scope_key(comp_b) not in html


# --------------------------------------------------------------------------- #
# Venue page: exact single-competition module
# --------------------------------------------------------------------------- #
async def test_venue_page_shows_exact_competition_module(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The venue page renders exactly one competition:<id> profile."""
    comp_id = await _seed_box_complete_competition(
        db_session, year=2025, venue_slug="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        result = await rebuild_environment_profiles(db_session, competition_id=comp_id)
    assert result.failed_scopes == 0

    resp = await app_client.get("/stats/summer-league/2025/las_vegas")
    assert resp.status_code == 200
    html = resp.text

    assert "Competition profile" in html
    assert competition_scope_key(comp_id) in html
    assert "/stats/summer-league/explorer?subject=competitions" in html
    assert "profile_scope=competition" in html
    assert f"competition_id={comp_id}" in html
    assert "Pace (per 48)" in html


# --------------------------------------------------------------------------- #
# Absent profile: neutral unavailable state, never an error
# --------------------------------------------------------------------------- #
async def test_season_and_venue_show_unavailable_state_when_no_profile_published(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Raw Summer League data with no rebuilt profile shows a neutral hint."""
    await _seed_box_complete_competition(
        db_session, year=2026, venue_slug="las_vegas", league_id="15"
    )
    await db_session.commit()
    # Deliberately never call rebuild_environment_profiles.

    season_resp = await app_client.get("/stats/summer-league/2026")
    assert season_resp.status_code == 200
    assert (
        "Competition Context profiles have not been published for 2026 yet."
        in season_resp.text
    )

    venue_resp = await app_client.get("/stats/summer-league/2026/las_vegas")
    assert venue_resp.status_code == 200
    assert (
        "A Competition Context profile has not been published for this "
        "competition yet." in venue_resp.text
    )


# --------------------------------------------------------------------------- #
# Collision case: identical venue_slug across two years must never leak
# --------------------------------------------------------------------------- #
async def test_venue_page_no_cross_year_leakage_for_shared_venue_slug(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two competitions sharing a venue_slug across years never cross-link.

    Both editions use the *same* ``venue_slug`` ("las_vegas") in different
    years, with deliberately different team-box totals so their profiles are
    numerically distinct. The venue page must resolve strictly by the page's
    own canonical ``competition_id`` (contract: never by label alone).
    """
    comp_2024 = await _seed_box_complete_competition(
        db_session,
        year=2024,
        venue_slug="las_vegas",
        league_id="15",
        line_a=_team_line(pts=120, fga=90, fg3a=40, tov=10),
        line_b=_team_line(pts=100, fga=88, fg3a=30, tov=14),
    )
    comp_2025 = await _seed_box_complete_competition(
        db_session,
        year=2025,
        venue_slug="las_vegas",
        league_id="15",
        line_a=_team_line(pts=80, fga=80, fg3a=15, tov=22),
        line_b=_team_line(pts=75, fga=82, fg3a=12, tov=20),
    )
    await db_session.commit()

    async with db_session.begin():
        r2024 = await rebuild_environment_profiles(db_session, competition_id=comp_2024)
    async with db_session.begin():
        r2025 = await rebuild_environment_profiles(db_session, competition_id=comp_2025)
    assert r2024.failed_scopes == 0 and r2025.failed_scopes == 0
    assert comp_2024 != comp_2025

    resp_2024 = await app_client.get("/stats/summer-league/2024/las_vegas")
    resp_2025 = await app_client.get("/stats/summer-league/2025/las_vegas")
    assert resp_2024.status_code == 200
    assert resp_2025.status_code == 200
    html_2024, html_2025 = resp_2024.text, resp_2025.text

    # Each page shows its own scope key and never the other edition's.
    assert competition_scope_key(comp_2024) in html_2024
    assert competition_scope_key(comp_2025) not in html_2024
    assert competition_scope_key(comp_2025) in html_2025
    assert competition_scope_key(comp_2024) not in html_2025

    # Each page's Explorer link is scoped to its own competition_id.
    assert f"competition_id={comp_2024}" in html_2024
    assert f"competition_id={comp_2025}" not in html_2024
    assert f"competition_id={comp_2025}" in html_2025
    assert f"competition_id={comp_2024}" not in html_2025

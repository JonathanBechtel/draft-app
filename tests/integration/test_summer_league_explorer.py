"""Integration tests for the Summer League Explorer (Phase 1: players subject).

Explorer (`/stats/summer-league/explorer`): a faceted, URL-encoded query builder.
Covers query parsing/validation, players aggregation + scope filters, sorting,
pagination, the partial (JS-swap) render path, and the not-yet-available subjects.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    parse_query,
    run_explorer_query,
)
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _comp(db: AsyncSession, *, year: int, venue_slug: str, league_id: str) -> int:
    _N["i"] += 1
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp.id


async def _team(db: AsyncSession, *, comp_id: int) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    t = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"ex-team-{_N['i']}",
        raw_team_name="Team",
        team_slug=f"team-{_N['i']}",
    )
    db.add(t)
    await db.flush()
    return t


async def _log(
    db: AsyncSession,
    *,
    comp_id: int,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    pts: int,
    games: int = 1,
    round_label: str | None = None,
) -> None:
    """Add ``games`` identical box lines (30 MIN each) for one player."""
    for _ in range(games):
        _N["i"] += 1
        g = SummerLeagueGame(
            competition_id=comp_id,
            nba_stats_game_id=f"ex-game-{_N['i']}",
            game_date=date(2024, 7, 3),
            home_team_entry_id=team.id,
            away_team_entry_id=team.id,
            home_score=100,
            away_score=90,
            round_label=round_label,
        )
        db.add(g)
        await db.flush()
        assert g.id is not None
        sp = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"ex-person-{_N['i']}",
            raw_player_name=player.display_name or "Player",
            normalized_name=(player.display_name or "player").lower(),
            canonical_player_id=player.id,
        )
        db.add(sp)
        await db.flush()
        db.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp_id,
                game_id=g.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                raw_player_name=player.display_name or "Player",
                minutes_seconds=1800,
                pts=pts,
                reb=5,
                ast=3,
                fgm=pts // 2,
                fga=pts,
            )
        )
    await db.flush()


async def _seed(db: AsyncSession) -> None:
    """Two players across two years/venues, each with 2 GP so they qualify.

    Scorer: 30 PPG (2024 Vegas). Roleplayer: 10 PPG (2025 Salt Lake).
    """
    scorer = make_player("Big", "Scorer")
    role = make_player("Role", "Player")
    scorer.draft_year, scorer.draft_round = 2024, 1
    role.draft_year, role.draft_round = 2025, 2
    db.add_all([scorer, role])
    await db.flush()

    c1 = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t1 = await _team(db, comp_id=c1)
    await _log(db, comp_id=c1, team=t1, player=scorer, pts=30, games=2)

    c2 = await _comp(db, year=2025, venue_slug="salt_lake_city", league_id="16")
    t2 = await _team(db, comp_id=c2)
    await _log(db, comp_id=c2, team=t2, player=role, pts=10, games=2)
    await db.commit()


# --------------------------------------------------------------------------- #
# parse_query
# --------------------------------------------------------------------------- #


def test_parse_query_defaults_and_validation() -> None:
    """Unknown subject/mode/sort fall back to safe defaults; ints coerce."""
    q = parse_query({"subject": "aliens", "mode": "warp", "sort": "evil", "dir": "x"})
    assert q.subject == "players"
    assert q.mode == "per_game"
    assert q.sort == "pts"
    assert q.direction == "desc"

    q2 = parse_query({"year_min": "2021", "year_max": "bad", "page": "-3"})
    assert q2.year_min == 2021
    assert q2.year_max is None  # invalid → filter off
    assert q2.page == 1  # clamped to >= 1


# --------------------------------------------------------------------------- #
# players subject
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_players_aggregates_and_sorts(db_session: AsyncSession) -> None:
    """Default players query returns qualifying players sorted by PTS desc."""
    await _seed(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))

    assert result.available is True
    assert result.total == 2
    assert [r.label for r in result.rows] == ["Big Scorer", "Role Player"]
    assert result.rows[0].values["pts"] == 30.0
    assert result.rows[0].href == "/players/big-scorer"
    # Shooting volume is surfaced as discrete made/attempted columns.
    col_keys = {c.key for c in result.columns}
    assert {"fgm", "fga", "fg3m", "fg3a", "ftm", "fta"} <= col_keys
    assert "fgm" in result.rows[0].values


@pytest.mark.asyncio
async def test_players_venue_filter_narrows(db_session: AsyncSession) -> None:
    """Venue scope drops players who only appear at other venues."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", venue="las_vegas")
    )
    assert [r.label for r in result.rows] == ["Big Scorer"]


@pytest.mark.asyncio
async def test_players_draft_round_filter(db_session: AsyncSession) -> None:
    """Draft-round scope keeps only players from that round."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", draft_round=2)
    )
    assert [r.label for r in result.rows] == ["Role Player"]


@pytest.mark.asyncio
async def test_players_ascending_sort(db_session: AsyncSession) -> None:
    """Ascending sort flips the order."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", sort="pts", direction="asc")
    )
    assert [r.label for r in result.rows] == ["Role Player", "Big Scorer"]


@pytest.mark.asyncio
async def test_totals_mode_sums_box_score(db_session: AsyncSession) -> None:
    """Totals mode reports summed counting stats (30 PPG x 2 GP = 60 PTS)."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", mode="totals")
    )
    top = result.rows[0]
    assert top.label == "Big Scorer"
    assert top.values["pts"] == 60  # summed, not per-game


@pytest.mark.asyncio
async def test_per_36_mode_scales_by_minutes(db_session: AsyncSession) -> None:
    """Per-36 mode scales counting stats to a 36-minute rate (30 min played)."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", mode="per_36")
    )
    top = result.rows[0]
    # 60 total pts over 60 total min -> 1.0 pts/min -> 36.0 per 36.
    assert top.values["pts"] == 36.0


@pytest.mark.asyncio
async def test_min_minutes_filter_excludes_small_samples(
    db_session: AsyncSession,
) -> None:
    """A high min-minutes floor excludes everyone below it."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", min_minutes=10_000)
    )
    assert result.total == 0


# --------------------------------------------------------------------------- #
# subjects not yet available
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_facets_populate_for_every_subject(db_session: AsyncSession) -> None:
    """Builder facets populate regardless of subject."""
    await _seed(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="games"))
    assert result.facets.years == [2025, 2024]


# --------------------------------------------------------------------------- #
# teams subject (Phase 2)
# --------------------------------------------------------------------------- #


async def _team_game(
    db: AsyncSession,
    *,
    comp_id: int,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int,
    away_score: int,
    pace: float = 100.0,
) -> None:
    """A real game between two distinct teams, with a box log per side."""
    _N["i"] += 1
    g = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"tm-game-{_N['i']}",
        game_date=date(2024, 7, 4),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(g)
    await db.flush()
    assert g.id is not None
    for entry, pts, opp in (
        (home, home_score, away_score),
        (away, away_score, home_score),
    ):
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp_id,
                game_id=g.id,
                team_entry_id=entry.id,
                pts=pts,
                plus_minus=pts - opp,
                pace=pace,
                off_rating=110.0,
                def_rating=105.0,
            )
        )
    await db.flush()


async def _seed_teams(db: AsyncSession) -> None:
    """One competition, two teams; Alpha beats Bravo twice (Alpha 2-0, Bravo 0-2)."""
    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    alpha = await _team(db, comp_id=c)
    bravo = await _team(db, comp_id=c)
    alpha.raw_team_name, bravo.raw_team_name = "Alpha", "Bravo"
    await db.flush()
    await _team_game(
        db, comp_id=c, home=alpha, away=bravo, home_score=110, away_score=90
    )
    await _team_game(
        db, comp_id=c, home=bravo, away=alpha, home_score=95, away_score=105
    )
    await db.commit()


@pytest.mark.asyncio
async def test_teams_aggregates_record_from_scores(db_session: AsyncSession) -> None:
    """Teams subject computes W-L and scoring from game scores, sorted by DIFF."""
    await _seed_teams(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="teams"))

    assert result.available is True
    assert result.total == 2
    top = result.rows[0]
    assert top.label.startswith("Alpha")
    assert (top.values["w"], top.values["l"]) == (2, 0)
    assert top.values["ppg"] == 107.5  # (110 + 105) / 2
    assert top.values["opp_ppg"] == 92.5  # (90 + 95) / 2
    assert top.values["diff"] == 15.0
    assert top.values["pace"] == 100.0  # averaged from box logs
    # Bravo is the mirror image and sorts second (lower DIFF).
    assert result.rows[1].label.startswith("Bravo")
    assert result.rows[1].values["diff"] == -15.0


@pytest.mark.asyncio
async def test_teams_links_to_team_season(db_session: AsyncSession) -> None:
    """Each team row links to its team-season page."""
    await _seed_teams(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="teams"))
    assert result.rows[0].href is not None
    assert result.rows[0].href.startswith("/stats/summer-league/2024/las_vegas/")


@pytest.mark.asyncio
async def test_teams_default_sort_is_valid_for_subject(
    db_session: AsyncSession,
) -> None:
    """A player sort key passed to the teams subject falls back to the team default."""
    await _seed_teams(db_session)
    q = parse_query({"subject": "teams", "sort": "ts_pct"})
    assert q.sort == "diff"  # 'ts_pct' is not a team column


# --------------------------------------------------------------------------- #
# games subject (Phase 3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_games_one_row_per_game_with_total_and_margin(
    db_session: AsyncSession,
) -> None:
    """Games subject yields one row per scored game with total + margin."""
    await _seed_teams(db_session)  # 2 games: 110-90 and 105-95
    result = await run_explorer_query(db_session, ExplorerQuery(subject="games"))

    assert result.available is True
    assert result.total == 2
    assert {r.values["total"] for r in result.rows} == {200}
    assert {r.values["margin"] for r in result.rows} == {20, 10}


@pytest.mark.asyncio
async def test_games_sort_by_margin(db_session: AsyncSession) -> None:
    """Sorting by margin desc puts the bigger blowout first, linked to its box."""
    await _seed_teams(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="games", sort="margin", direction="desc")
    )
    assert result.rows[0].values["margin"] == 20
    assert result.rows[0].href is not None
    assert result.rows[0].href.startswith("/stats/summer-league/2024/games/")
    # The label carries date + matchup + score.
    assert "@" in result.rows[0].label


@pytest.mark.asyncio
async def test_games_default_sort_is_total(db_session: AsyncSession) -> None:
    """The games subject defaults its sort key to 'total'."""
    q = parse_query({"subject": "games"})
    assert q.sort == "total"


@pytest.mark.asyncio
async def test_games_venue_filter(db_session: AsyncSession) -> None:
    """A non-matching venue filter yields no games."""
    await _seed_teams(db_session)  # all games at las_vegas
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="games", venue="orlando")
    )
    assert result.total == 0


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explorer_page_renders(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """Full page renders the builder, subject tabs, and a seeded result."""
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200
    body = resp.text
    assert "Explorer" in body
    assert "explorer-form" in body
    assert "Big Scorer" in body


@pytest.mark.asyncio
async def test_explorer_partial_returns_table_only(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """?partial=1 returns just the results fragment (no full document)."""
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer?partial=1")
    assert resp.status_code == 200
    body = resp.text
    assert "explorer-results" in body
    assert "<html" not in body.lower()
    assert "explorer-form" not in body  # the builder is not in the partial


@pytest.mark.asyncio
async def test_explorer_not_shadowed_by_year_route(app_client: AsyncClient) -> None:
    """`/explorer` must hit the explorer route, not 422 against `/{year:int}`."""
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Phase 1: position filter, undrafted filter, positions facet, plus_minus
# --------------------------------------------------------------------------- #


async def _seed_with_positions(db: AsyncSession) -> None:
    """Three players: one G (drafted), one F (drafted), one undrafted with no position."""
    guard = make_player("Guard", "One")
    guard.position = "G"
    guard.draft_year, guard.draft_round = 2024, 1

    forward = make_player("Forward", "Two")
    forward.position = "F"
    forward.draft_year, forward.draft_round = 2024, 2

    undrafted = make_player("Undrafted", "Three")
    undrafted.position = None
    undrafted.draft_year = None

    db.add_all([guard, forward, undrafted])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(db, comp_id=c, team=t, player=guard, pts=20, games=2)
    await _log(db, comp_id=c, team=t, player=forward, pts=15, games=2)
    await _log(db, comp_id=c, team=t, player=undrafted, pts=10, games=2)
    await db.commit()


@pytest.mark.asyncio
async def test_position_filter_returns_only_matching_position(
    db_session: AsyncSession,
) -> None:
    """?position=G returns only players at that position; others are excluded."""
    await _seed_with_positions(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", position="G")
    )
    assert result.total == 1
    assert result.rows[0].label == "Guard One"


@pytest.mark.asyncio
async def test_undrafted_filter_returns_only_undrafted(
    db_session: AsyncSession,
) -> None:
    """?undrafted=1 returns only players with no draft year."""
    await _seed_with_positions(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", undrafted=True)
    )
    assert result.total == 1
    assert result.rows[0].label == "Undrafted Three"


@pytest.mark.asyncio
async def test_positions_facet_lists_distinct_values(
    db_session: AsyncSession,
) -> None:
    """Positions facet enumerates distinct non-null position values from PlayerMaster."""
    await _seed_with_positions(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    assert "F" in result.facets.positions
    assert "G" in result.facets.positions
    # None is excluded from the facet
    assert None not in result.facets.positions  # type: ignore[operator]


@pytest.mark.asyncio
async def test_plus_minus_suppressed_in_rate_modes(
    db_session: AsyncSession,
) -> None:
    """plus_minus is None for per_36 and per_100 modes, present for per_game/totals."""
    await _seed(db_session)
    for mode in ("per_36", "per_100"):
        result = await run_explorer_query(
            db_session, ExplorerQuery(subject="players", mode=mode)
        )
        assert result.rows[0].values["plus_minus"] is None, f"expected None in {mode}"

    for mode in ("per_game", "totals"):
        result = await run_explorer_query(
            db_session, ExplorerQuery(subject="players", mode=mode)
        )
        assert result.rows[0].values["plus_minus"] is not None, (
            f"expected value in {mode}"
        )


# --------------------------------------------------------------------------- #
# Phase 2g: round type filter
# --------------------------------------------------------------------------- #


async def _seed_round_types(db: AsyncSession) -> None:
    """Two players: one in Qualifying games, one in Championship games.

    Each player has 2 GP so they exceed the default min_games threshold.
    """
    qualifier = make_player("Qual", "Player")
    champion = make_player("Champ", "Player")
    db.add_all([qualifier, champion])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(
        db,
        comp_id=c,
        team=t,
        player=qualifier,
        pts=10,
        games=2,
        round_label="Qualifying",
    )
    await _log(
        db,
        comp_id=c,
        team=t,
        player=champion,
        pts=20,
        games=2,
        round_label="Championship",
    )
    await db.commit()


@pytest.mark.asyncio
async def test_round_type_filter_players(db_session: AsyncSession) -> None:
    """round_type filter narrows player results to games with that round_label.

    Seeding one player in Qualifying games and one in Championship games, then
    filtering to Qualifying should return only the qualifying player.
    """
    await _seed_round_types(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", round_type="Qualifying", min_games=1),
    )
    assert result.total == 1
    assert result.rows[0].label == "Qual Player"


@pytest.mark.asyncio
async def test_round_type_filter_games(db_session: AsyncSession) -> None:
    """round_type filter on the games subject yields 0 rows when no match exists.

    All seeded games via _seed_teams have no round_label; filtering to
    'Championship' should return an empty result set.
    """
    await _seed_teams(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="games", round_type="Championship"),
    )
    assert result.total == 0


@pytest.mark.asyncio
async def test_round_types_facet_lists_values(db_session: AsyncSession) -> None:
    """Facets include distinct non-null round_label values from SummerLeagueGame.

    After seeding games with 'Qualifying' and 'Championship' labels, both values
    should appear in facets.round_types (alphabetically sorted), while None is
    excluded.
    """
    await _seed_round_types(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    assert "Championship" in result.facets.round_types
    assert "Qualifying" in result.facets.round_types
    # Alphabetical order
    assert result.facets.round_types == sorted(result.facets.round_types)
